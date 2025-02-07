"""Module implements the connector to interact with the Skoda API."""
from __future__ import annotations
from typing import TYPE_CHECKING

import threading

import json
import os
import logging
import netrc
from datetime import datetime, timezone, timedelta
import requests

from carconnectivity.garage import Garage
from carconnectivity.errors import AuthenticationError, TooManyRequestsError, RetrievalError, APIError, APICompatibilityError, \
    TemporaryAuthenticationError, ConfigurationError, SetterError, CommandError
from carconnectivity.util import robust_time_parse, log_extra_keys, config_remove_credentials
from carconnectivity.units import Length, Power, Speed
from carconnectivity.vehicle import GenericVehicle
from carconnectivity.doors import Doors
from carconnectivity.windows import Windows
from carconnectivity.lights import Lights
from carconnectivity.drive import GenericDrive, ElectricDrive, CombustionDrive
from carconnectivity.attributes import BooleanAttribute, DurationAttribute, GenericAttribute, TemperatureAttribute
from carconnectivity.units import Temperature
from carconnectivity.command_impl import ClimatizationStartStopCommand, WakeSleepCommand, HonkAndFlashCommand, LockUnlockCommand, ChargingStartStopCommand
from carconnectivity.climatization import Climatization
from carconnectivity.commands import Commands
from carconnectivity.charging import Charging

from carconnectivity_connectors.base.connector import BaseConnector
from carconnectivity_connectors.volkswagen.auth.session_manager import SessionManager, SessionUser, Service
from carconnectivity_connectors.volkswagen.auth.we_connect_session import WeConnectSession
from carconnectivity_connectors.volkswagen.vehicle import VolkswagenVehicle, VolkswagenElectricVehicle, VolkswagenCombustionVehicle, \
    VolkswagenHybridVehicle
from carconnectivity_connectors.volkswagen.climatization import VolkswagenClimatization
from carconnectivity_connectors.volkswagen.capability import Capability
from carconnectivity_connectors.volkswagen._version import __version__
from carconnectivity_connectors.volkswagen.command_impl import SpinCommand
from carconnectivity_connectors.volkswagen.charging import VolkswagenCharging, mapping_volskwagen_charging_state


if TYPE_CHECKING:
    from typing import Dict, List, Optional, Any, Union

    from carconnectivity.carconnectivity import CarConnectivity

LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.volkswagen")
LOG_API: logging.Logger = logging.getLogger("carconnectivity.connectors.volkswagen-api-debug")


# pylint: disable=too-many-lines
class Connector(BaseConnector):
    """
    Connector class for Skoda API connectivity.
    Args:
        car_connectivity (CarConnectivity): An instance of CarConnectivity.
        config (Dict): Configuration dictionary containing connection details.
    Attributes:
        max_age (Optional[int]): Maximum age for cached data in seconds.
    """
    def __init__(self, connector_id: str, car_connectivity: CarConnectivity, config: Dict) -> None:
        BaseConnector.__init__(self, connector_id=connector_id, car_connectivity=car_connectivity, config=config)

        self._background_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self.connected: BooleanAttribute = BooleanAttribute(name="connected", parent=self)
        self.interval: DurationAttribute = DurationAttribute(name="interval", parent=self)
        self.commands: Commands = Commands(parent=self)

        # Configure logging
        if 'log_level' in config and config['log_level'] is not None:
            config['log_level'] = config['log_level'].upper()
            if config['log_level'] in logging._nameToLevel:
                LOG.setLevel(config['log_level'])
                self.log_level._set_value(config['log_level'])  # pylint: disable=protected-access
                logging.getLogger('requests').setLevel(config['log_level'])
                logging.getLogger('urllib3').setLevel(config['log_level'])
                logging.getLogger('oauthlib').setLevel(config['log_level'])
            else:
                raise ConfigurationError(f'Invalid log level: "{config["log_level"]}" not in {list(logging._nameToLevel.keys())}')
        if 'api_log_level' in config and config['api_log_level'] is not None:
            config['api_log_level'] = config['api_log_level'].upper()
            if config['api_log_level'] in logging._nameToLevel:
                LOG_API.setLevel(config['api_log_level'])
            else:
                raise ConfigurationError(f'Invalid log level: "{config["log_level"]}" not in {list(logging._nameToLevel.keys())}')
        LOG.info("Loading volkswagen connector with config %s", config_remove_credentials(self.config))

        if 'spin' in config and config['spin'] is not None:
            self._spin: Optional[str] = config['spin']
        else:
            self._spin = None

        username: Optional[str] = None
        password: Optional[str] = None
        if 'username' in self.config and 'password' in self.config:
            username = self.config['username']
            password = self.config['password']
        else:
            if 'netrc' in self.config:
                netrc_filename: str = self.config['netrc']
            else:
                netrc_filename = os.path.join(os.path.expanduser("~"), ".netrc")
            try:
                secrets = netrc.netrc(file=netrc_filename)
                secret: tuple[str, str, str] | None = secrets.authenticators("volkswagen")
                if secret is None:
                    raise AuthenticationError(f'Authentication using {netrc_filename} failed: volkswagen not found in netrc')
                username, account, password = secret

                if self._spin is None and account is not None:
                    try:
                        self._spin = account
                    except ValueError as err:
                        LOG.error('Could not parse spin from netrc: %s', err)
            except netrc.NetrcParseError as err:
                LOG.error('Authentification using %s failed: %s', netrc_filename, err)
                raise AuthenticationError(f'Authentication using {netrc_filename} failed: {err}') from err
            except TypeError as err:
                if 'username' not in self.config:
                    raise AuthenticationError(f'"volkswagen" entry was not found in {netrc_filename} netrc-file.'
                                              ' Create it or provide username and password in config') from err
            except FileNotFoundError as err:
                raise AuthenticationError(f'{netrc_filename} netrc-file was not found. Create it or provide username and password in config') from err

        interval: int = 300
        if 'interval' in self.config:
            interval = self.config['interval']
            if interval < 180:
                raise ValueError('Intervall must be at least 180 seconds')
        self.max_age: int = interval - 1
        if 'max_age' in self.config:
            self.max_age = self.config['max_age']
        self.interval._set_value(timedelta(seconds=interval))  # pylint: disable=protected-access

        if username is None or password is None:
            raise AuthenticationError('Username or password not provided')

        self._manager: SessionManager = SessionManager(tokenstore=car_connectivity.get_tokenstore(), cache=car_connectivity.get_cache())
        session: requests.Session = self._manager.get_session(Service.WE_CONNECT, SessionUser(username=username, password=password))
        if not isinstance(session, WeConnectSession):
            raise AuthenticationError('Could not create session')
        self.session: WeConnectSession = session
        self.session.retries = 3
        self.session.timeout = 180
        self.session.refresh()

        self._elapsed: List[timedelta] = []

    def startup(self) -> None:
        self._background_thread = threading.Thread(target=self._background_loop, daemon=False)
        self._background_thread.start()

    def _background_loop(self) -> None:
        self._stop_event.clear()
        while not self._stop_event.is_set():
            interval = 300
            try:
                try:
                    self.fetch_all()
                    self.last_update._set_value(value=datetime.now(tz=timezone.utc))  # pylint: disable=protected-access
                    if self.interval.value is not None:
                        interval: float = self.interval.value.total_seconds()
                except Exception:
                    self.connected._set_value(value=False)  # pylint: disable=protected-access
                    if self.interval.value is not None:
                        interval: float = self.interval.value.total_seconds()
                    raise
            except TooManyRequestsError as err:
                LOG.error('Retrieval error during update. Too many requests from your account (%s). Will try again after 15 minutes', str(err))
                self._stop_event.wait(900)
            except RetrievalError as err:
                LOG.error('Retrieval error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self._stop_event.wait(interval)
            except APICompatibilityError as err:
                LOG.error('API compatability error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self._stop_event.wait(interval)
            except TemporaryAuthenticationError as err:
                LOG.error('Temporary authentification error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self._stop_event.wait(interval)
            else:
                self.connected._set_value(value=True)  # pylint: disable=protected-access
                self._stop_event.wait(interval)

    def persist(self) -> None:
        """
        Persists the current state using the manager's persist method.

        This method calls the `persist` method of the `_manager` attribute to save the current state.
        """
        self._manager.persist()

    def shutdown(self) -> None:
        """
        Shuts down the connector by persisting current state, closing the session,
        and cleaning up resources.

        This method performs the following actions:
        1. Persists the current state.
        2. Closes the session.
        3. Sets the session and manager to None.
        4. Calls the shutdown method of the base connector.

        Returns:
            None
        """
        # Disable and remove all vehicles managed soley by this connector
        for vehicle in self.car_connectivity.garage.list_vehicles():
            if len(vehicle.managing_connectors) == 1 and self in vehicle.managing_connectors:
                self.car_connectivity.garage.remove_vehicle(vehicle.id)
                vehicle.enabled = False
        self._stop_event.set()
        if self._background_thread is not None:
            self._background_thread.join()
        self.persist()
        self.session.close()
        BaseConnector.shutdown(self)

    def fetch_all(self) -> None:
        """
        Fetches all necessary data for the connector.

        This method calls the `fetch_vehicles` method to retrieve vehicle data.
        """
        # Add spin command
        if self.commands is not None and not self.commands.contains_command('spin'):
            spin_command = SpinCommand(parent=self.commands)
            spin_command._add_on_set_hook(self.__on_spin)  # pylint: disable=protected-access
            spin_command.enabled = True
            self.commands.add_command(spin_command)
        self.fetch_vehicles()
        self.car_connectivity.transaction_end()

    def fetch_vehicles(self) -> None:
        """
        Fetches the list of vehicles from the Skoda Connect API and updates the garage with new vehicles.
        This method sends a request to the Skoda Connect API to retrieve the list of vehicles associated with the user's account.
        If new vehicles are found in the response, they are added to the garage.

        Returns:
            None
        """
        garage: Garage = self.car_connectivity.garage
        url = 'https://emea.bff.cariad.digital/vehicle/v1/vehicles'
        data: Dict[str, Any] | None = self._fetch_data(url, session=self.session)

        seen_vehicle_vins: set[str] = set()
        if data is not None:
            if 'data' in data and data['data'] is not None:
                for vehicle_dict in data['data']:
                    if 'vin' in vehicle_dict and vehicle_dict['vin'] is not None:
                        seen_vehicle_vins.add(vehicle_dict['vin'])
                        vehicle: Optional[VolkswagenVehicle] = garage.get_vehicle(vehicle_dict['vin'])  # pyright: ignore[reportAssignmentType]
                        if vehicle is None:
                            vehicle = VolkswagenVehicle(vin=vehicle_dict['vin'], garage=garage, managing_connector=self)
                            garage.add_vehicle(vehicle_dict['vin'], vehicle)

                        if 'nickname' in vehicle_dict and vehicle_dict['nickname'] is not None:
                            vehicle.name._set_value(vehicle_dict['nickname'])  # pylint: disable=protected-access
                        else:
                            vehicle.name._set_value(None)  # pylint: disable=protected-access

                        if 'model' in vehicle_dict and vehicle_dict['model'] is not None:
                            vehicle.model._set_value(vehicle_dict['model'])  # pylint: disable=protected-access
                        else:
                            vehicle.model._set_value(None)  # pylint: disable=protected-access

                        if 'capabilities' in vehicle_dict and vehicle_dict['capabilities'] is not None:
                            found_capabilities = set()
                            for capability_dict in vehicle_dict['capabilities']:
                                if 'id' in capability_dict and capability_dict['id'] is not None:
                                    capability_id = capability_dict['id']
                                    found_capabilities.add(capability_id)
                                    if vehicle.capabilities.has_capability(capability_id):
                                        capability: Capability = vehicle.capabilities.get_capability(capability_id)  # pyright: ignore[reportAssignmentType]
                                    else:
                                        capability = Capability(capability_id=capability_id, capabilities=vehicle.capabilities)
                                        vehicle.capabilities.add_capability(capability_id, capability)
                                    if 'expirationDate' in capability_dict and capability_dict['expirationDate'] is not None:
                                        expiration_date: datetime = robust_time_parse(capability_dict['expirationDate'])
                                        capability.expiration_date._set_value(expiration_date)  # pylint: disable=protected-access
                                    else:
                                        capability.expiration_date._set_value(None)  # pylint: disable=protected-access
                                    if 'userDisablingAllowed' in capability_dict and capability_dict['userDisablingAllowed'] is not None:
                                        # pylint: disable-next=protected-access
                                        capability.user_disabling_allowed._set_value(capability_dict['userDisablingAllowed'])
                                    else:
                                        capability.user_disabling_allowed._set_value(None)  # pylint: disable=protected-access
                                else:
                                    raise APIError('Could not fetch capabilities, capability ID missing')
                            for capability_id in vehicle.capabilities.capabilities.keys() - found_capabilities:
                                vehicle.capabilities.remove_capability(capability_id)
                        else:
                            vehicle.capabilities.clear_capabilities()

                        if vehicle.capabilities.has_capability('vehicleWakeUpTrigger'):
                            if vehicle.commands is not None and vehicle.commands.commands is not None \
                                    and not vehicle.commands.contains_command('wake-sleep'):
                                wake_sleep_command = WakeSleepCommand(parent=vehicle.commands)
                                wake_sleep_command._add_on_set_hook(self.__on_wake_sleep)  # pylint: disable=protected-access
                                wake_sleep_command.enabled = True
                                vehicle.commands.add_command(wake_sleep_command)

                        # Add honkAndFlash command if necessary capabilities are available
                        if vehicle.capabilities.has_capability('honkAndFlash'):
                            if vehicle.commands is not None and vehicle.commands.commands is not None \
                                    and not vehicle.commands.contains_command('honk-flash'):
                                honk_flash_command = HonkAndFlashCommand(parent=vehicle.commands, with_duration=True)
                                honk_flash_command._add_on_set_hook(self.__on_honk_flash)  # pylint: disable=protected-access
                                honk_flash_command.enabled = True
                                vehicle.commands.add_command(honk_flash_command)

                        # Add lock and unlock command
                        if vehicle.capabilities.has_capability('access'):
                            if vehicle.doors is not None and vehicle.doors.commands is not None and vehicle.doors.commands.commands is not None \
                                    and not vehicle.doors.commands.contains_command('lock-unlock'):
                                lock_unlock_command = LockUnlockCommand(parent=vehicle.doors.commands)
                                lock_unlock_command._add_on_set_hook(self.__on_lock_unlock)  # pylint: disable=protected-access
                                lock_unlock_command.enabled = True
                                vehicle.doors.commands.add_command(lock_unlock_command)

                        self.fetch_vehicle_status(vehicle)
                        if vehicle.capabilities.has_capability('parkingPosition'):
                            self.fetch_parking_position(vehicle)
                    else:
                        raise APIError('Could not fetch vehicle data, VIN missing')
        for vin in set(garage.list_vehicle_vins()) - seen_vehicle_vins:
            vehicle_to_remove = garage.get_vehicle(vin)
            if vehicle_to_remove is not None and vehicle_to_remove.is_managed_by_connector(self):
                garage.remove_vehicle(vin)

    def fetch_vehicle_status(self, vehicle: VolkswagenVehicle) -> None:
        """
        Fetches the status of a vehicle from the Volkswagen API.

        Args:
            vehicle (GenericVehicle): The vehicle object containing the VIN.

        Returns:
            None
        """
        vin = vehicle.vin.value
        if vin is None:
            raise ValueError('vehicle.vin cannot be None')
        known_capabilities: list[str] = ['access',
                                         'activeventilation',
                                         'automation',
                                         'auxiliaryheating',
                                         'userCapabilities'
                                         'charging',
                                         'chargingProfiles',
                                         'batteryChargingCare',
                                         'climatisation',
                                         'climatisationTimers'
                                         'departureTimers',
                                         'fuelStatus',
                                         'vehicleLights',
                                         'lvBattery',
                                         'readiness',
                                         'vehicleHealthInspection',
                                         'vehicleHealthWarnings',
                                         'oilLevel',
                                         'measurements',
                                         'batterySupport']
        jobs: list[str] = []
        for capability_id in known_capabilities:
            if vehicle.capabilities.has_capability(capability_id) \
                    and vehicle.capabilities.get_capability(capability_id).enabled:  # pyright: ignore[reportOptionalMemberAccess]
                jobs.append(capability_id)
        if len(jobs) == 0:
            LOG.warning('No capabilities enabled for vehicle %s', vin)
            return

        url = f'https://emea.bff.cariad.digital/vehicle/v1/vehicles/{vin}/selectivestatus?jobs=' + ','.join(jobs)
        data: Dict[str, Any] | None = self._fetch_data(url, self.session)
        if data is not None:
            if 'measurements' in data and data['measurements'] is not None:
                if 'fuelLevelStatus' in data['measurements'] and data['measurements']['fuelLevelStatus'] is not None:
                    if 'value' in data['measurements']['fuelLevelStatus'] and data['measurements']['fuelLevelStatus']['value'] is not None:
                        fuel_level_status = data['measurements']['fuelLevelStatus']['value']
                        captured_at: datetime = robust_time_parse(fuel_level_status['carCapturedTimestamp'])
                        # Check vehicle type and if it does not match the current vehicle type, create a new vehicle object using copy constructor
                        if 'carType' in fuel_level_status and fuel_level_status['carType'] is not None:
                            try:
                                car_type = GenericVehicle.Type(fuel_level_status['carType'])
                                if car_type == GenericVehicle.Type.ELECTRIC and not isinstance(vehicle, VolkswagenElectricVehicle):
                                    LOG.debug('Promoting %s to VolkswagenElectricVehicle object for %s', vehicle.__class__.__name__, vin)
                                    vehicle = VolkswagenElectricVehicle(origin=vehicle)
                                    self.car_connectivity.garage.replace_vehicle(vin, vehicle)
                                elif car_type in [GenericVehicle.Type.FUEL,
                                                  GenericVehicle.Type.GASOLINE,
                                                  GenericVehicle.Type.PETROL,
                                                  GenericVehicle.Type.DIESEL,
                                                  GenericVehicle.Type.CNG,
                                                  GenericVehicle.Type.LPG] \
                                        and not isinstance(vehicle, VolkswagenCombustionVehicle):
                                    LOG.debug('Promoting %s to VolkswagenCombustionVehicle object for %s', vehicle.__class__.__name__, vin)
                                    vehicle = VolkswagenCombustionVehicle(origin=vehicle)
                                    self.car_connectivity.garage.replace_vehicle(vin, vehicle)
                                elif car_type == GenericVehicle.Type.HYBRID and not isinstance(vehicle, VolkswagenHybridVehicle):
                                    LOG.debug('Promoting %s to VolkswagenHybridVehicle object for %s', vehicle.__class__.__name__, vin)
                                    vehicle = VolkswagenHybridVehicle(origin=vehicle)
                                    self.car_connectivity.garage.replace_vehicle(vin, vehicle)
                                vehicle.type._set_value(car_type)  # pylint: disable=protected-access
                            except ValueError:
                                LOG_API.warning('Unknown car type %s', fuel_level_status['carType'])
                        log_extra_keys(LOG_API, 'fuelLevelStatus', fuel_level_status, {'carCapturedTimestamp', 'carType'})
                if 'odometerStatus' in data['measurements'] and data['measurements']['odometerStatus'] is not None:
                    if 'value' in data['measurements']['odometerStatus'] and data['measurements']['odometerStatus']['value'] is not None:
                        odometer_status = data['measurements']['odometerStatus']['value']
                        if 'carCapturedTimestamp' not in odometer_status or odometer_status['carCapturedTimestamp'] is None:
                            raise APIError('Could not fetch vehicle status, carCapturedTimestamp missing')
                        captured_at: datetime = robust_time_parse(odometer_status['carCapturedTimestamp'])
                        if 'odometer' in odometer_status and odometer_status['odometer'] is not None:
                            # pylint: disable-next=protected-access
                            vehicle.odometer._set_value(value=odometer_status['odometer'], measured=captured_at, unit=Length.KM)
                        else:
                            vehicle.odometer._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                        log_extra_keys(LOG_API, 'odometerStatus', odometer_status, {'carCapturedTimestamp', 'odometer'})
                else:
                    vehicle.odometer._set_value(None)  # pylint: disable=protected-access
                log_extra_keys(LOG_API, 'measurements', data['measurements'], {'fuelLevelStatus', 'odometerStatus'})
            else:
                vehicle.odometer._set_value(None)  # pylint: disable=protected-access
            if 'fuelStatus' in data and data['fuelStatus'] is not None:
                if 'rangeStatus' in data['fuelStatus'] and data['fuelStatus']['rangeStatus'] is not None:
                    if 'value' in data['fuelStatus']['rangeStatus'] and data['fuelStatus']['rangeStatus']['value'] is not None:
                        range_status = data['fuelStatus']['rangeStatus']['value']
                        if 'carCapturedTimestamp' not in range_status or range_status['carCapturedTimestamp'] is None:
                            raise APIError('Could not fetch vehicle status, carCapturedTimestamp missing')
                        captured_at: datetime = robust_time_parse(range_status['carCapturedTimestamp'])
                        drive_ids: set[str] = {'primary', 'secondary'}
                        for drive_id in drive_ids:
                            if f'{drive_id}Engine' in range_status and range_status[f'{drive_id}Engine'] is not None:
                                try:
                                    engine_type: GenericDrive.Type = GenericDrive.Type(range_status[f'{drive_id}Engine']['type'])
                                except ValueError:
                                    LOG_API.warning('Unknown engine_type type %s', range_status[f'{drive_id}Engine']['type'])
                                    engine_type: GenericDrive.Type = GenericDrive.Type.UNKNOWN

                                if drive_id in vehicle.drives.drives:
                                    drive: GenericDrive = vehicle.drives.drives[drive_id]
                                else:
                                    if engine_type == GenericDrive.Type.ELECTRIC:
                                        drive = ElectricDrive(drive_id=drive_id, drives=vehicle.drives)
                                    elif engine_type in [GenericDrive.Type.FUEL,
                                                         GenericDrive.Type.GASOLINE,
                                                         GenericDrive.Type.PETROL,
                                                         GenericDrive.Type.DIESEL,
                                                         GenericDrive.Type.CNG,
                                                         GenericDrive.Type.LPG]:
                                        drive = CombustionDrive(drive_id=drive_id, drives=vehicle.drives)
                                    else:
                                        drive = GenericDrive(drive_id=drive_id, drives=vehicle.drives)
                                    drive.type._set_value(engine_type)  # pylint: disable=protected-access
                                    vehicle.drives.add_drive(drive)
                                if 'currentSOC_pct' in range_status[f'{drive_id}Engine'] \
                                        and range_status[f'{drive_id}Engine']['currentSOC_pct'] is not None:
                                    # pylint: disable-next=protected-access
                                    drive.level._set_value(value=range_status[f'{drive_id}Engine']['currentSOC_pct'], measured=captured_at)
                                elif 'currentFuelLevel_pct' in range_status[f'{drive_id}Engine'] \
                                        and range_status[f'{drive_id}Engine']['currentFuelLevel_pct'] is not None:
                                    # pylint: disable-next=protected-access
                                    drive.level._set_value(value=range_status[f'{drive_id}Engine']['currentFuelLevel_pct'], measured=captured_at)
                                else:
                                    drive.level._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                                if 'remainingRange_km' in range_status[f'{drive_id}Engine'] \
                                        and range_status[f'{drive_id}Engine']['remainingRange_km'] is not None:
                                    # pylint: disable-next=protected-access
                                    drive.range._set_value(value=range_status[f'{drive_id}Engine']['remainingRange_km'], measured=captured_at, unit=Length.KM)
                                else:
                                    drive.range._set_value(None, measured=captured_at, unit=Length.KM)  # pylint: disable=protected-access

                                log_extra_keys(LOG_API, f'{drive_id}Engine', range_status[f'{drive_id}Engine'], {'type',
                                                                                                                 'currentSOC_pct',
                                                                                                                 'currentFuelLevel_pct'
                                                                                                                 'remainingRange_km'})
                        log_extra_keys(LOG_API, 'rangeStatus', range_status, {'carCapturedTimestamp', 'primaryEngine', 'secondaryEngine'})
                    else:
                        vehicle.drives.enabled = False
                else:
                    vehicle.drives.enabled = False
            else:
                vehicle.drives.enabled = False

            if 'access' in data and data['access'] is not None:
                if 'accessStatus' in data['access'] and data['access']['accessStatus'] is not None:
                    if 'value' in data['access']['accessStatus'] and data['access']['accessStatus']['value'] is not None:
                        access_status = data['access']['accessStatus']['value']
                        if 'carCapturedTimestamp' not in access_status or access_status['carCapturedTimestamp'] is None:
                            raise APIError('Could not fetch vehicle status, carCapturedTimestamp missing')
                        captured_at: datetime = robust_time_parse(access_status['carCapturedTimestamp'])
                        seen_door_ids: set[str] = set()
                        if 'doors' in access_status and access_status['doors'] is not None:
                            all_doors_closed = True
                            for door_status in access_status['doors']:
                                if 'name' in door_status and door_status['name'] is not None:
                                    door_id = door_status['name']
                                    seen_door_ids.add(door_id)
                                    if door_id in vehicle.doors.doors:
                                        door: Doors.Door = vehicle.doors.doors[door_id]
                                    else:
                                        door = Doors.Door(door_id=door_id, doors=vehicle.doors)
                                        vehicle.doors.doors[door_id] = door
                                    if 'status' in door_status and door_status['status'] is not None:
                                        if 'locked' in door_status['status']:
                                            door.lock_state._set_value(Doors.LockState.LOCKED, measured=captured_at)  # pylint: disable=protected-access
                                        elif 'unlocked' in door_status['status']:
                                            door.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
                                        else:
                                            door.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                                        if 'open' in door_status['status']:
                                            all_doors_closed = False
                                            door.open_state._set_value(Doors.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
                                        elif 'closed' in door_status['status']:
                                            door.open_state._set_value(Doors.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                                        elif 'unsupported' in door_status['status']:
                                            door.open_state._set_value(Doors.OpenState.UNSUPPORTED, measured=captured_at)  # pylint: disable=protected-access
                                        else:
                                            door.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                                            LOG_API.info('Unknown door status %s', door_status['status'])
                                    else:
                                        door.open_state._set_value(None)  # pylint: disable=protected-access
                                        door.lock_state._set_value(None)  # pylint: disable=protected-access
                                else:
                                    raise APIError('Could not fetch door status, door ID missing')
                                log_extra_keys(LOG_API, 'doors', door_status, {'name', 'status'})
                            if all_doors_closed:
                                vehicle.doors.open_state._set_value(Doors.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                            else:
                                vehicle.doors.open_state._set_value(Doors.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
                            if 'doorLockStatus' in access_status and access_status['doorLockStatus'] is not None:
                                if access_status['doorLockStatus'] == 'locked':
                                    vehicle.doors.lock_state._set_value(Doors.LockState.LOCKED, measured=captured_at)  # pylint: disable=protected-access
                                elif access_status['doorLockStatus'] == 'unlocked':
                                    vehicle.doors.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
                                elif access_status['doorLockStatus'] == 'invalid':
                                    vehicle.doors.lock_state._set_value(Doors.LockState.INVALID, measured=captured_at)  # pylint: disable=protected-access
                                else:
                                    LOG_API.info('Unknown door lock status %s', access_status['doorLockStatus'])
                                    vehicle.doors.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                            else:
                                vehicle.doors.lock_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access

                        else:
                            vehicle.doors.open_state._set_value(None)  # pylint: disable=protected-access
                        for door_id in vehicle.doors.doors.keys() - seen_door_ids:
                            vehicle.doors.doors[door_id].enabled = False
                        if 'overallStatus' in access_status and access_status['overallStatus'] is not None:
                            if access_status['overallStatus'] == 'safe':
                                vehicle.doors.lock_state._set_value(Doors.LockState.LOCKED, measured=captured_at)  # pylint: disable=protected-access
                            elif access_status['overallStatus'] == 'unsafe':
                                vehicle.doors.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
                        else:
                            vehicle.doors.lock_state._set_value(None)  # pylint: disable=protected-access
                        seen_window_ids: set[str] = set()
                        if 'windows' in access_status and access_status['windows'] is not None:
                            all_windows_closed = True
                            for window_status in access_status['windows']:
                                if 'name' in window_status and window_status['name'] is not None:
                                    window_id = window_status['name']
                                    seen_window_ids.add(window_id)
                                    if window_id in vehicle.windows.windows:
                                        window: Windows.Window = vehicle.windows.windows[window_id]
                                    else:
                                        window = Windows.Window(window_id=window_id, windows=vehicle.windows)
                                        vehicle.windows.windows[window_id] = window
                                    if 'status' in window_status and window_status['status'] is not None:
                                        if 'closed' in window_status['status']:
                                            window.open_state._set_value(Windows.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                                        elif 'open' in window_status['status']:
                                            all_windows_closed = False
                                            window.open_state._set_value(Windows.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
                                        elif 'unsupported' in window_status['status']:
                                            # pylint: disable-next=protected-access
                                            window.open_state._set_value(Windows.OpenState.UNSUPPORTED, measured=captured_at)
                                        elif 'invalid' in window_status['status']:
                                            window.open_state._set_value(Windows.OpenState.INVALID, measured=captured_at)  # pylint: disable=protected-access
                                        else:
                                            window.open_state._set_value(Windows.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                                            LOG_API.info('Unknown window status %s', window_status['status'])
                                    else:
                                        window.open_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                                else:
                                    raise APIError('Could not fetch window status, window ID missing')
                            if all_windows_closed:
                                vehicle.windows.open_state._set_value(Windows.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                            else:
                                vehicle.windows.open_state._set_value(Windows.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
                        else:
                            vehicle.windows.open_state._set_value(None)  # pylint: disable=protected-access
                        for window_id in vehicle.windows.windows.keys() - seen_window_ids:
                            vehicle.windows.windows[window_id].enabled = False
                        log_extra_keys(LOG_API, 'accessStatus', access_status, {'carCapturedTimestamp',
                                                                                'doors',
                                                                                'overallStatus',
                                                                                'doorLockStatus',
                                                                                'windows'})
                log_extra_keys(LOG_API, 'access', data['access'], {'accessStatus'})
            else:
                vehicle.doors.lock_state._set_value(None)  # pylint: disable=protected-access
                vehicle.doors.open_state._set_value(None)  # pylint: disable=protected-access
                vehicle.doors.enabled = False
            if 'vehicleLights' in data and data['vehicleLights'] is not None:
                if 'lightsStatus' in data['vehicleLights'] and data['vehicleLights']['lightsStatus'] is not None:
                    lights_status = data['vehicleLights']['lightsStatus']
                    seen_light_ids: set[str] = set()
                    if 'value' in lights_status and lights_status['value'] is not None:
                        lights_status = lights_status['value']
                        if 'carCapturedTimestamp' not in lights_status or lights_status['carCapturedTimestamp'] is None:
                            raise APIError('Could not fetch vehicle status, carCapturedTimestamp missing')
                        captured_at: datetime = robust_time_parse(lights_status['carCapturedTimestamp'])
                        if 'lights' in lights_status and lights_status['lights'] is not None:
                            all_lights_off = True
                            for light_status in lights_status['lights']:
                                if 'name' in light_status and light_status['name'] is not None:
                                    light_id = light_status['name']
                                    seen_light_ids.add(light_id)
                                    if light_id in vehicle.lights.lights:
                                        light: Lights.Light = vehicle.lights.lights[light_id]
                                    else:
                                        light: Lights.Light = Lights.Light(light_id=light_id, lights=vehicle.lights)
                                        vehicle.lights.lights[light_id] = light
                                    if 'status' in light_status and light_status['status'] is not None:
                                        if light_status['status'] == 'on':
                                            all_lights_off = False
                                            light.light_state._set_value(Lights.LightState.ON, measured=captured_at)  # pylint: disable=protected-access
                                        elif light_status['status'] == 'off':
                                            light.light_state._set_value(Lights.LightState.OFF, measured=captured_at)  # pylint: disable=protected-access
                                        elif light_status['status'] == 'invalid':
                                            light.light_state._set_value(Lights.LightState.INVALID, measured=captured_at)  # pylint: disable=protected-access
                                        else:
                                            light.light_state._set_value(Lights.LightState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                                            LOG_API.info('Unknown light status %s', light_status['status'])
                                    else:
                                        light.light_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                                else:
                                    raise APIError('Could not fetch light status, light ID missing')
                                log_extra_keys(LOG_API, 'lights', light_status, {'name', 'status'})
                            if all_lights_off:
                                vehicle.lights.light_state._set_value(Lights.LightState.OFF, measured=captured_at)  # pylint: disable=protected-access
                            else:
                                vehicle.lights.light_state._set_value(Lights.LightState.ON, measured=captured_at)  # pylint: disable=protected-access
                        else:
                            vehicle.lights.light_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                        log_extra_keys(LOG_API, 'lights', lights_status, {'carCapturedTimestamp', 'lights'})
                    for light_id in vehicle.lights.lights.keys() - seen_light_ids:
                        vehicle.lights.lights[light_id].enabled = False
                else:
                    vehicle.lights.light_state._set_value(None)  # pylint: disable=protected-access
                    vehicle.lights.enabled = False
            else:
                vehicle.lights.light_state._set_value(None)  # pylint: disable=protected-access
                vehicle.lights.enabled = False
            if 'climatisation' in data and data['climatisation'] is not None:
                if not isinstance(vehicle.climatization, VolkswagenClimatization):
                    vehicle.climatization = VolkswagenClimatization(origin=vehicle.climatization)
                assert isinstance(vehicle.climatization.settings, VolkswagenClimatization.Settings)
                if vehicle.climatization is not None and vehicle.climatization.commands is not None \
                        and not vehicle.climatization.commands.contains_command('start-stop'):
                    start_stop_command = ClimatizationStartStopCommand(parent=vehicle.climatization.commands)
                    start_stop_command._add_on_set_hook(self.__on_air_conditioning_start_stop)  # pylint: disable=protected-access
                    start_stop_command.enabled = True
                    vehicle.climatization.commands.add_command(start_stop_command)
                if 'climatisationStatus' in data['climatisation'] and data['climatisation']['climatisationStatus'] is not None:
                    climatisation_status = data['climatisation']['climatisationStatus']
                    if 'value' in climatisation_status and climatisation_status['value'] is not None:
                        climatisation_status = climatisation_status['value']
                        if 'carCapturedTimestamp' not in climatisation_status or climatisation_status['carCapturedTimestamp'] is None:
                            raise APIError('Could not fetch vehicle status, carCapturedTimestamp missing')
                        captured_at: datetime = robust_time_parse(climatisation_status['carCapturedTimestamp'])
                        if 'climatisationState' in climatisation_status and climatisation_status['climatisationState'] is not None:
                            if climatisation_status['climatisationState'] in VolkswagenClimatization.ClimatizationState:
                                climatization_state: VolkswagenClimatization.ClimatizationState = \
                                    VolkswagenClimatization.ClimatizationState(climatisation_status['climatisationState'])
                            else:
                                LOG_API.info('Unknown climatization state %s not in %s', climatisation_status['climatisationState'],
                                             str(VolkswagenClimatization.ClimatizationState))
                                climatization_state = VolkswagenClimatization.ClimatizationState.UNKNOWN
                            vehicle.climatization.state._set_value(value=climatization_state, measured=captured_at)  # pylint: disable=protected-access
                        else:
                            vehicle.climatization.state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                        if 'remainingClimatisationTime_min' in climatisation_status and climatisation_status['remainingClimatisationTime_min'] is not None:
                            remaining_duration: timedelta = timedelta(minutes=climatisation_status['remainingClimatisationTime_min'])
                            estimated_date_reached: datetime = captured_at + remaining_duration
                            estimated_date_reached = estimated_date_reached.replace(second=0, microsecond=0)
                            # pylint: disable-next=protected-access
                            vehicle.climatization.estimated_date_reached._set_value(value=estimated_date_reached, measured=captured_at)
                        else:
                            vehicle.climatization.estimated_date_reached._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                        log_extra_keys(LOG_API, 'climatisationStatus', climatisation_status, {'carCapturedTimestamp', 'climatisationState',
                                                                                              'remainingClimatisationTime_min'})
                else:
                    vehicle.climatization.state._set_value(None)  # pylint: disable=protected-access
                    vehicle.climatization.estimated_date_reached._set_value(None)  # pylint: disable=protected-access
                if 'climatisationSettings' in data['climatisation'] and data['climatisation']['climatisationSettings'] is not None:
                    climatisation_settings = data['climatisation']['climatisationSettings']
                    if 'value' in climatisation_settings and climatisation_settings['value'] is not None:
                        climatisation_settings = climatisation_settings['value']
                        if 'carCapturedTimestamp' not in climatisation_settings or climatisation_settings['carCapturedTimestamp'] is None:
                            raise APIError('Could not fetch vehicle status, carCapturedTimestamp missing')
                        captured_at: datetime = robust_time_parse(climatisation_settings['carCapturedTimestamp'])
                        preferred_unit: Temperature = Temperature.C
                        if 'unitInCar' in climatisation_settings and climatisation_settings['unitInCar'] is not None:
                            if climatisation_settings['unitInCar'] == 'farenheit':
                                preferred_unit = Temperature.F
                                vehicle.climatization.settings.unit_in_car = Temperature.F
                            elif climatisation_settings['unitInCar'] == 'celsius':
                                preferred_unit = Temperature.C
                                vehicle.climatization.settings.unit_in_car = Temperature.C
                            else:
                                LOG_API.info('Unknown unitInCar %s', climatisation_settings['unitInCar'])
                        if preferred_unit == Temperature.C and 'targetTemperature_C' in climatisation_settings:
                            target_temperature: Optional[float] = climatisation_settings['targetTemperature_C']
                            actual_unit: Optional[Temperature] = Temperature.C
                        elif preferred_unit == Temperature.F and 'targetTemperature_F' in climatisation_settings:
                            target_temperature = climatisation_settings['targetTemperature_F']
                            actual_unit = Temperature.F
                        elif 'targetTemperature_C' in climatisation_settings:
                            target_temperature = climatisation_settings['targetTemperature_C']
                            actual_unit = Temperature.C
                        elif 'targetTemperature_F' in climatisation_settings:
                            target_temperature = climatisation_settings['targetTemperature_F']
                            actual_unit = Temperature.F
                        else:
                            target_temperature = None
                            actual_unit = None
                        vehicle.climatization.settings.target_temperature._set_value(value=target_temperature,  # pylint: disable=protected-access
                                                                                     measured=captured_at,
                                                                                     unit=actual_unit)
                        # pylint: disable-next=protected-access
                        vehicle.climatization.settings.target_temperature._add_on_set_hook(self.__on_air_conditioning_settings_change)
                        vehicle.climatization.settings.target_temperature._is_changeable = True  # pylint: disable=protected-access
                        if 'climatisationWithoutExternalPower' in climatisation_settings \
                                and climatisation_settings['climatisationWithoutExternalPower'] is not None:
                            vehicle.climatization.settings.climatization_without_external_power._set_value(  # pylint: disable=protected-access
                                climatisation_settings['climatisationWithoutExternalPower'], measured=captured_at)
                            # pylint: disable-next=protected-access
                            vehicle.climatization.settings.climatization_without_external_power._add_on_set_hook(self.__on_air_conditioning_settings_change)
                            vehicle.climatization.settings.climatization_without_external_power._is_changeable = True  # pylint: disable=protected-access
                        else:
                            # pylint: disable-next=protected-access
                            vehicle.climatization.settings.climatization_without_external_power._set_value(None, measured=captured_at)
                        if 'climatizationAtUnlock' in climatisation_settings and climatisation_settings['climatizationAtUnlock'] is not None:
                            vehicle.climatization.settings.climatization_at_unlock._set_value(  # pylint: disable=protected-access
                                climatisation_settings['climatizationAtUnlock'], measured=captured_at)
                            # pylint: disable-next=protected-access
                            vehicle.climatization.settings.climatization_at_unlock._add_on_set_hook(self.__on_air_conditioning_settings_change)
                            vehicle.climatization.settings.climatization_at_unlock._is_changeable = True  # pylint: disable=protected-access
                        else:
                            # pylint: disable-next=protected-access
                            vehicle.climatization.settings.climatization_at_unlock._set_value(None, measured=captured_at)
                        if 'windowHeatingEnabled' in climatisation_settings and climatisation_settings['windowHeatingEnabled'] is not None:
                            vehicle.climatization.settings.window_heating._set_value(  # pylint: disable=protected-access
                                climatisation_settings['windowHeatingEnabled'], measured=captured_at)
                        # pylint: disable-next=protected-access
                            vehicle.climatization.settings.window_heating._add_on_set_hook(self.__on_air_conditioning_settings_change)
                            vehicle.climatization.settings.window_heating._is_changeable = True  # pylint: disable=protected-access
                        else:
                            # pylint: disable-next=protected-access
                            vehicle.climatization.settings.window_heating._set_value(None, measured=captured_at)
                        if 'zoneFrontLeftEnabled' in climatisation_settings and climatisation_settings['zoneFrontLeftEnabled'] is not None:
                            vehicle.climatization.settings.front_zone_left_enabled._set_value(  # pylint: disable=protected-access
                                climatisation_settings['zoneFrontLeftEnabled'], measured=captured_at)
                            # pylint: disable-next=protected-access
                            vehicle.climatization.settings.front_zone_left_enabled._add_on_set_hook(self.__on_air_conditioning_settings_change)
                            vehicle.climatization.settings.front_zone_left_enabled._is_changeable = True  # pylint: disable=protected-access
                        else:
                            # pylint: disable-next=protected-access
                            vehicle.climatization.settings.front_zone_left_enabled._set_value(None, measured=captured_at)
                        if 'zoneFrontRightEnabled' in climatisation_settings and climatisation_settings['zoneFrontRightEnabled'] is not None:
                            vehicle.climatization.settings.front_zone_right_enabled._set_value(  # pylint: disable=protected-access
                                climatisation_settings['zoneFrontRightEnabled'], measured=captured_at)
                            # pylint: disable-next=protected-access
                            vehicle.climatization.settings.front_zone_right_enabled._add_on_set_hook(self.__on_air_conditioning_settings_change)
                            vehicle.climatization.settings.front_zone_right_enabled._is_changeable = True  # pylint: disable=protected-access
                        else:
                            # pylint: disable-next=protected-access
                            vehicle.climatization.settings.front_zone_right_enabled._set_value(None, measured=captured_at)
                        if 'rearZoneLeftEnabled' in climatisation_settings and climatisation_settings['rearZoneLeftEnabled'] is not None:
                            vehicle.climatization.settings.rear_zone_left_enabled._set_value(  # pylint: disable=protected-access
                                climatisation_settings['rearZoneLeftEnabled'], measured=captured_at)
                            # pylint: disable-next=protected-access
                            vehicle.climatization.settings.rear_zone_left_enabled._add_on_set_hook(self.__on_air_conditioning_settings_change)
                            vehicle.climatization.settings.rear_zone_left_enabled._is_changeable = True  # pylint: disable=protected-access
                        else:
                            # pylint: disable-next=protected-access
                            vehicle.climatization.settings.rear_zone_left_enabled._set_value(None, measured=captured_at)
                        if 'rearZoneRightEnabled' in climatisation_settings and climatisation_settings['rearZoneRightEnabled'] is not None:
                            vehicle.climatization.settings.rear_zone_right_enabled._set_value(  # pylint: disable=protected-access
                                climatisation_settings['rearZoneRightEnabled'], measured=captured_at)
                            # pylint: disable-next=protected-access
                            vehicle.climatization.settings.rear_zone_right_enabled._add_on_set_hook(self.__on_air_conditioning_settings_change)
                            vehicle.climatization.settings.rear_zone_right_enabled._is_changeable = True  # pylint: disable=protected-access
                        else:
                            # pylint: disable-next=protected-access
                            vehicle.climatization.settings.rear_zone_right_enabled._set_value(None, measured=captured_at)
                        if vehicle.climatization.settings.front_zone_left_enabled.enabled \
                                or vehicle.climatization.settings.front_zone_right_enabled.enabled \
                                or vehicle.climatization.settings.rear_zone_left_enabled.enabled \
                                or vehicle.climatization.settings.rear_zone_right_enabled.enabled:
                            if vehicle.climatization.settings.front_zone_left_enabled.value \
                                    or vehicle.climatization.settings.front_zone_right_enabled.value \
                                    or vehicle.climatization.settings.rear_zone_left_enabled.value \
                                    or vehicle.climatization.settings.rear_zone_right_enabled.value:
                                vehicle.climatization.settings.seat_heating._set_value(True, measured=captured_at)  # pylint: disable=protected-access
                            else:
                                vehicle.climatization.settings.seat_heating._set_value(False, measured=captured_at)  # pylint: disable=protected-access
                        else:
                            vehicle.climatization.settings.seat_heating._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                        if 'heaterSource' in climatisation_settings and climatisation_settings['heaterSource'] is not None:
                            if climatisation_settings['heaterSource'] in Climatization.Settings.HeaterSource:
                                vehicle.climatization.settings.heater_source._set_value(  # pylint: disable=protected-access
                                    Climatization.Settings.HeaterSource(climatisation_settings['heaterSource']), measured=captured_at)
                            else:
                                LOG_API.info('Unknown heater source %s', climatisation_settings['heaterSource'])
                                # pylint: disable-next=protected-access
                                vehicle.climatization.settings.heater_source._set_value(Climatization.Settings.HeaterSource.UNKNOWN, measured=captured_at)
                        else:
                            vehicle.climatization.settings.heater_source._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                        log_extra_keys(LOG_API, 'climatisationSettings', climatisation_settings, {'carCapturedTimestamp',
                                                                                                  'unitInCar',
                                                                                                  'targetTemperature_C',
                                                                                                  'targetTemperature_F',
                                                                                                  'climatisationWithoutExternalPower',
                                                                                                  'climatizationAtUnlock',
                                                                                                  'windowHeatingEnabled',
                                                                                                  'zoneFrontLeftEnabled',
                                                                                                  'zoneFrontRightEnabled',
                                                                                                  'heaterSource'})
                else:
                    vehicle.climatization.settings.target_temperature._set_value(None)  # pylint: disable=protected-access
                    vehicle.climatization.settings.climatization_without_external_power._set_value(None)  # pylint: disable=protected-access
                    vehicle.climatization.settings.climatization_at_unlock._set_value(None)  # pylint: disable=protected-access
                    vehicle.climatization.settings.window_heating._set_value(None)  # pylint: disable=protected-access
                    vehicle.climatization.settings.front_zone_left_enabled._set_value(None)  # pylint: disable=protected-access
                    vehicle.climatization.settings.front_zone_right_enabled._set_value(None)  # pylint: disable=protected-access
                    vehicle.climatization.settings.rear_zone_left_enabled._set_value(None)  # pylint: disable=protected-access
                    vehicle.climatization.settings.rear_zone_right_enabled._set_value(None)  # pylint: disable=protected-access
                    vehicle.climatization.settings.seat_heating._set_value(None)  # pylint: disable=protected-access
                    vehicle.climatization.settings.heater_source._set_value(None)  # pylint: disable=protected-access

                log_extra_keys(LOG_API, 'climatisation', data['climatisation'], {'climatisationStatus', 'climatisationSettings'})
            if 'charging' in data and data['charging'] is not None:
                if not isinstance(vehicle, VolkswagenElectricVehicle):
                    vehicle = VolkswagenElectricVehicle(origin=vehicle)
                    self.car_connectivity.garage.replace_vehicle(vin, vehicle)
                if vehicle.charging is not None and vehicle.charging.commands is not None \
                        and not vehicle.charging.commands.contains_command('start-stop'):
                    start_stop_command = ChargingStartStopCommand(parent=vehicle.charging.commands)
                    start_stop_command._add_on_set_hook(self.__on_charging_start_stop)  # pylint: disable=protected-access
                    start_stop_command.enabled = True
                    vehicle.charging.commands.add_command(start_stop_command)
                if 'chargingStatus' in data['charging'] and data['charging']['chargingStatus'] is not None:
                    charging_status = data['climatisation']['chargingStatus']
                    if 'value' in charging_status and charging_status['value'] is not None:
                        climatisation_status = charging_status['value']
                        if 'carCapturedTimestamp' not in charging_status or charging_status['carCapturedTimestamp'] is None:
                            raise APIError('Could not fetch vehicle status, carCapturedTimestamp missing')
                        captured_at: datetime = robust_time_parse(charging_status['carCapturedTimestamp'])
                        if 'chargingState' in charging_status and charging_status['chargingState'] is not None:
                            if charging_status['chargingState'] in VolkswagenCharging.VolkswagenChargingState:
                                volkswagen_charging_state = VolkswagenCharging.VolkswagenChargingState(charging_status['chargingState'])
                                charging_state: Charging.ChargingState = mapping_volskwagen_charging_state[volkswagen_charging_state]
                            else:
                                LOG_API.info('Unkown charging state %s not in %s', charging_status['chargingState'],
                                             str(VolkswagenCharging.VolkswagenChargingState))
                                charging_state = Charging.ChargingState.UNKNOWN

                            # pylint: disable-next=protected-access
                            vehicle.charging.state._set_value(value=charging_state, measured=captured_at)
                        else:
                            vehicle.charging.state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                        if 'chargeType' in charging_status and charging_status['chargeType'] is not None:
                            if charging_status['chargeType'] in Charging.ChargingType:
                                vehicle.charging.type._set_value(value=Charging.ChargingType(charging_status['chargeType']),  # pylint: disable=protected-access
                                                                 measured=captured_at)
                            else:
                                LOG_API.info('Unknown charge type %s', charging_status['chargeType'])
                                vehicle.charging.type._set_value(Charging.ChargingType.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                        else:
                            vehicle.charging.type._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                        if 'chargePower_kW' in charging_status and charging_status['chargePower_kW'] is not None:
                            vehicle.charging.power._set_value(value=charging_status['chargePower_kW'],  # pylint: disable=protected-access
                                                              measured=captured_at, unit=Power.KW)
                        else:
                            vehicle.charging.power._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                        if 'chargeRate_kmph' in charging_status and charging_status['chargeRate_kmph'] is not None:
                            vehicle.charging.rate._set_value(value=charging_status['chargeRate_kmph'],  # pylint: disable=protected-access
                                                             measured=captured_at, unit=Speed.KMH)
                        else:
                            vehicle.charging.rate._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                        if 'remainingTimeToComplete_min' in charging_status and charging_status['remainingTimeToComplete_min'] is not None:
                            remaining_duration: timedelta = timedelta(minutes=charging_status['remainingTimeToComplete_min'])
                            estimated_date_reached: datetime = captured_at + remaining_duration
                            estimated_date_reached = estimated_date_reached.replace(second=0, microsecond=0)
                            vehicle.charging.estimated_date_reached._set_value(value=estimated_date_reached,  # pylint: disable=protected-access
                                                                               measured=captured_at)
                        else:
                            vehicle.charging.estimated_date_reached._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                        log_extra_keys(LOG_API, 'charging', data['charging'], {'chargingStatus', 'carCapturedTimestamp', 'chargingState', 'chargePower_kW',
                                                                               'chargeRate_kmph', 'remainingTimeToComplete_min'})
                    log_extra_keys(LOG_API, 'chargingStatus', charging_status, {'carCapturedTimestamp'})
            log_extra_keys(LOG_API, 'selectivestatus', data, {'measurements', 'access', 'vehicleLights', 'climatisation'})

    def fetch_parking_position(self, vehicle: VolkswagenVehicle) -> None:
        """
        Fetches the parking position of the given Volkswagen vehicle and updates the vehicle's position attributes.

        Args:
            vehicle (VolkswagenVehicle): The Volkswagen vehicle object whose parking position is to be fetched.

        Raises:
            ValueError: If the vehicle's VIN is None.
            APIError: If the fetched data does not contain 'carCapturedTimestamp' or it is None.

        Updates:
            vehicle.position.latitude: The latitude of the vehicle's parking position.
            vehicle.position.longitude: The longitude of the vehicle's parking position.
        """
        vin = vehicle.vin.value
        if vin is None:
            raise ValueError('vehicle.vin cannot be None')
        url: str = f'https://emea.bff.cariad.digital/vehicle/v1/vehicles/{vin}/parkingposition'
        data: Dict[str, Any] | None = self._fetch_data(url, self.session)
        if data is not None and 'data' in data and data['data'] is not None:
            if 'carCapturedTimestamp' not in data['data'] or data['data']['carCapturedTimestamp'] is None:
                raise APIError('Could not fetch vehicle status, carCapturedTimestamp missing')
            captured_at: datetime = robust_time_parse(data['data']['carCapturedTimestamp'])

            if 'lat' in data['data'] and data['data']['lat'] is not None and 'lon' in data['data'] and data['data']['lon'] is not None:
                vehicle.position.latitude._set_value(data['data']['lat'], measured=captured_at)  # pylint: disable=protected-access
                vehicle.position.longitude._set_value(data['data']['lon'], measured=captured_at)  # pylint: disable=protected-access
            else:
                vehicle.position.latitude._set_value(None)  # pylint: disable=protected-access
                vehicle.position.longitude._set_value(None)  # pylint: disable=protected-access
        else:
            vehicle.position.latitude._set_value(None)  # pylint: disable=protected-access
            vehicle.position.longitude._set_value(None)  # pylint: disable=protected-access

    def _record_elapsed(self, elapsed: timedelta) -> None:
        """
        Records the elapsed time.

        Args:
            elapsed (timedelta): The elapsed time to record.
        """
        self._elapsed.append(elapsed)

    def _fetch_data(self, url, session, force=False, allow_empty=False, allow_http_error=False, allowed_errors=None) -> Optional[Dict[str, Any]]:  # noqa: C901
        data: Optional[Dict[str, Any]] = None
        cache_date: Optional[datetime] = None
        if not force and (self.max_age is not None and session.cache is not None and url in session.cache):
            data, cache_date_string = session.cache[url]
            cache_date = datetime.fromisoformat(cache_date_string)
        if data is None or self.max_age is None \
                or (cache_date is not None and cache_date < (datetime.utcnow() - timedelta(seconds=self.max_age))):
            try:
                status_response: requests.Response = session.get(url, allow_redirects=False)
                self._record_elapsed(status_response.elapsed)
                if status_response.status_code in (requests.codes['ok'], requests.codes['multiple_status']):
                    data = status_response.json()
                    if session.cache is not None:
                        session.cache[url] = (data, str(datetime.utcnow()))
                elif status_response.status_code == requests.codes['too_many_requests']:
                    raise TooManyRequestsError('Could not fetch data due to too many requests from your account. '
                                               f'Status Code was: {status_response.status_code}')
                elif status_response.status_code == requests.codes['unauthorized']:
                    LOG.info('Server asks for new authorization')
                    session.login()
                    status_response = session.get(url, allow_redirects=False)

                    if status_response.status_code in (requests.codes['ok'], requests.codes['multiple_status']):
                        data = status_response.json()
                        if session.cache is not None:
                            session.cache[url] = (data, str(datetime.utcnow()))
                    elif not allow_http_error or (allowed_errors is not None and status_response.status_code not in allowed_errors):
                        raise RetrievalError(f'Could not fetch data even after re-authorization. Status Code was: {status_response.status_code}')
                elif not allow_http_error or (allowed_errors is not None and status_response.status_code not in allowed_errors):
                    raise RetrievalError(f'Could not fetch data. Status Code was: {status_response.status_code}')
            except requests.exceptions.ConnectionError as connection_error:
                raise RetrievalError(f'Connection error: {connection_error}') from connection_error
            except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
                raise RetrievalError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
            except requests.exceptions.ReadTimeout as timeout_error:
                raise RetrievalError(f'Timeout during read: {timeout_error}') from timeout_error
            except requests.exceptions.RetryError as retry_error:
                raise RetrievalError(f'Retrying failed: {retry_error}') from retry_error
            except requests.exceptions.JSONDecodeError as json_error:
                if allow_empty:
                    data = None
                else:
                    raise RetrievalError(f'JSON decode error: {json_error}') from json_error
        return data

    def get_version(self) -> str:
        return __version__

    def get_type(self) -> str:
        return "carconnectivity-connector-volkswagen"

    def __on_air_conditioning_settings_change(self, attribute: GenericAttribute, value: Any) -> Any:
        """
        Callback for the climatization setting change.
        """
        if attribute.parent is None or not isinstance(attribute.parent, VolkswagenClimatization.Settings) \
                or attribute.parent.parent is None \
                or attribute.parent.parent.parent is None or not isinstance(attribute.parent.parent.parent, VolkswagenVehicle):
            raise SetterError('Object hierarchy is not as expected')
        settings: VolkswagenClimatization.Settings = attribute.parent
        vehicle: VolkswagenVehicle = attribute.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise SetterError('VIN in object hierarchy missing')
        setting_dict = {}
        if settings.target_temperature.enabled and settings.target_temperature.value is not None:
            # Round target temperature to nearest 0.5
            # Check if the attribute changed is the target_temperature attribute
            if isinstance(attribute, TemperatureAttribute) and attribute.id == 'target_temperature':
                setting_dict['targetTemperature'] = round(value * 2) / 2
            else:
                setting_dict['targetTemperature'] = round(settings.target_temperature.value * 2) / 2
            if settings.unit_in_car == Temperature.C:
                setting_dict['targetTemperatureUnit'] = 'celsius'
            elif settings.target_temperature.unit == Temperature.F:
                setting_dict['targetTemperatureUnit'] = 'farenheit'
            else:
                setting_dict['targetTemperatureUnit'] = 'celsius'
        if isinstance(attribute, BooleanAttribute) and attribute.id == 'climatisation_without_external_power':
            setting_dict['climatisationWithoutExternalPower'] = value
        elif settings.climatization_without_external_power.enabled and settings.climatization_without_external_power.value is not None:
            setting_dict['climatisationWithoutExternalPower'] = settings.climatization_without_external_power.value
        if isinstance(attribute, BooleanAttribute) and attribute.id == 'climatization_at_unlock':
            setting_dict['climatizationAtUnlock'] = value
        elif settings.climatization_at_unlock.enabled and settings.climatization_at_unlock.value is not None:
            setting_dict['climatizationAtUnlock'] = settings.climatization_at_unlock.value
        if isinstance(attribute, BooleanAttribute) and attribute.id == 'window_heating':
            setting_dict['windowHeatingEnabled'] = value
        elif settings.window_heating.enabled and settings.window_heating.value is not None:
            setting_dict['windowHeatingEnabled'] = settings.window_heating.value
        if isinstance(attribute, BooleanAttribute) and attribute.id == 'front_zone_left_enabled':
            setting_dict['zoneFrontLeftEnabled'] = value
        elif settings.front_zone_left_enabled.enabled and settings.front_zone_left_enabled.value is not None:
            setting_dict['zoneFrontLeftEnabled'] = settings.front_zone_left_enabled.value
        if isinstance(attribute, BooleanAttribute) and attribute.id == 'front_zone_right_enabled':
            setting_dict['zoneFrontRightEnabled'] = value
        elif settings.front_zone_right_enabled.enabled and settings.front_zone_right_enabled.value is not None:
            setting_dict['zoneFrontRightEnabled'] = settings.front_zone_right_enabled.value

        url: str = f'https://emea.bff.cariad.digital/vehicle/v1/vehicles/{vin}/climatisation/settings'
        settings_response: requests.Response = self.session.put(url, data=json.dumps(setting_dict), allow_redirects=True)
        if settings_response.status_code != requests.codes['ok']:
            LOG.error('Could not set climatization settings (%s)', settings_response.status_code)
            raise SetterError(f'Could not set value ({settings_response.status_code})')
        return value

    def __on_air_conditioning_start_stop(self, start_stop_command: ClimatizationStartStopCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if start_stop_command.parent is None or start_stop_command.parent.parent is None \
                or start_stop_command.parent.parent.parent is None or not isinstance(start_stop_command.parent.parent.parent, VolkswagenVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        vehicle: VolkswagenVehicle = start_stop_command.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        command_dict = {}
        command_str: Optional[str] = None
        if command_arguments['command'] == ClimatizationStartStopCommand.Command.START:
            command_str = 'start'
            if vehicle.climatization.settings is None:
                raise CommandError('Could not control climatisation, there are no climatisation settings for the vehicle available.')
            if 'target_temperature' in command_arguments:
                # Round target temperature to nearest 0.5
                command_dict['targetTemperature'] = round(command_arguments['target_temperature'] * 2) / 2
            elif vehicle.climatization.settings.target_temperature is not None and vehicle.climatization.settings.target_temperature.enabled \
                    and vehicle.climatization.settings.target_temperature.value is not None:
                if isinstance(vehicle.climatization.settings, VolkswagenClimatization.Settings) \
                        and vehicle.climatization.settings.unit_in_car is not None:
                    temperature_value: Optional[float] = vehicle.climatization.settings.target_temperature.temperature_in(
                        vehicle.climatization.settings.unit_in_car)
                else:
                    temperature_value = vehicle.climatization.settings.target_temperature.value
                if temperature_value is not None:
                    command_dict['targetTemperature'] = round(temperature_value * 2) / 2
            if 'target_temperature_unit' in command_arguments:
                if command_arguments['target_temperature_unit'] == Temperature.C:
                    command_dict['targetTemperatureUnit'] = 'celsius'
                elif command_arguments['target_temperature_unit'] == Temperature.F:
                    command_dict['targetTemperatureUnit'] = 'farenheit'
                else:
                    command_dict['targetTemperatureUnit'] = 'celsius'
            elif isinstance(vehicle.climatization.settings, VolkswagenClimatization.Settings) \
                    and vehicle.climatization.settings.unit_in_car == Temperature.C:
                command_dict['targetTemperatureUnit'] = 'celsius'
            elif isinstance(vehicle.climatization.settings, VolkswagenClimatization.Settings) \
                    and vehicle.climatization.settings.unit_in_car == Temperature.F:
                command_dict['targetTemperatureUnit'] = 'farenheit'
            else:
                command_dict['targetTemperatureUnit'] = 'celsius'

            if vehicle.climatization.settings.climatization_without_external_power is not None \
                    and vehicle.climatization.settings.climatization_without_external_power.enabled:
                command_dict['climatisationWithoutExternalPower'] = vehicle.climatization.settings.climatization_without_external_power.value
            if vehicle.climatization.settings.window_heating is not None and vehicle.climatization.settings.window_heating.enabled:
                command_dict['windowHeatingEnabled'] = vehicle.climatization.settings.window_heating.value
            if vehicle.climatization.settings.climatization_at_unlock is not None and vehicle.climatization.settings.climatization_at_unlock.enabled:
                command_dict['climatizationAtUnlock'] = vehicle.climatization.settings.climatization_at_unlock.value
            if isinstance(vehicle.climatization.settings, VolkswagenClimatization.Settings):
                if vehicle.climatization.settings.front_zone_left_enabled is not None and vehicle.climatization.settings.front_zone_left_enabled.enabled:
                    command_dict['zoneFrontLeftEnabled'] = vehicle.climatization.settings.front_zone_left_enabled.value
                if vehicle.climatization.settings.front_zone_right_enabled is not None and vehicle.climatization.settings.front_zone_right_enabled.enabled:
                    command_dict['zoneFrontRightEnabled'] = vehicle.climatization.settings.front_zone_right_enabled
                if vehicle.climatization.settings.rear_zone_left_enabled is not None and vehicle.climatization.settings.rear_zone_left_enabled.enabled:
                    command_dict['zoneRearLeftEnabled'] = vehicle.climatization.settings.rear_zone_left_enabled
                if vehicle.climatization.settings.rear_zone_right_enabled is not None and vehicle.climatization.settings.rear_zone_right_enabled.enabled:
                    command_dict['zoneRearRightEnabled'] = vehicle.climatization.settings.rear_zone_right_enabled
            if vehicle.climatization.settings.heater_source is not None and vehicle.climatization.settings.heater_source.enabled:
                command_dict['heaterSource'] = vehicle.climatization.settings.heater_source.value
        elif command_arguments['command'] == ClimatizationStartStopCommand.Command.STOP:
            command_str = 'stop'
        else:
            raise CommandError(f'Unknown command {command_arguments["command"]}')

        url: str = f'https://emea.bff.cariad.digital/vehicle/v1/vehicles/{vin}/climatisation/{command_str}'
        command_response: requests.Response = self.session.post(url, data=json.dumps(command_dict), allow_redirects=True)
        if command_response.status_code != requests.codes['ok']:
            LOG.error('Could not start/stop air conditioning (%s: %s)', command_response.status_code, command_response.text)
            raise CommandError(f'Could not start/stop air conditioning ({command_response.status_code}: {command_response.text})')
        return command_arguments

    def __on_wake_sleep(self, wake_sleep_command: WakeSleepCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if wake_sleep_command.parent is None or wake_sleep_command.parent.parent is None \
                or not isinstance(wake_sleep_command.parent.parent, GenericVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        vehicle: GenericVehicle = wake_sleep_command.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        if command_arguments['command'] == WakeSleepCommand.Command.WAKE:
            url = f'https://emea.bff.cariad.digital/vehicle/v1/vehicles/{vin}/vehiclewakeuptrigger'

            command_response: requests.Response = self.session.post(url, data='{}', allow_redirects=True)
            if command_response.status_code not in (requests.codes['ok'], requests.codes['no_content']):
                LOG.error('Could not execute wake command (%s: %s)', command_response.status_code, command_response.text)
                raise CommandError(f'Could not execute wake command ({command_response.status_code}: {command_response.text})')
        elif command_arguments['command'] == WakeSleepCommand.Command.SLEEP:
            raise CommandError('Sleep command not supported by vehicle. Vehicle will put itself to sleep')
        else:
            raise CommandError(f'Unknown command {command_arguments["command"]}')
        return command_arguments

    def __on_honk_flash(self, honk_flash_command: HonkAndFlashCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if honk_flash_command.parent is None or honk_flash_command.parent.parent is None \
                or not isinstance(honk_flash_command.parent.parent, GenericVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        vehicle: GenericVehicle = honk_flash_command.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        command_dict = {}
        if command_arguments['command'] in [HonkAndFlashCommand.Command.FLASH, HonkAndFlashCommand.Command.HONK_AND_FLASH]:
            if 'duration' in command_arguments:
                command_dict['duration_s'] = command_arguments['duration']
            else:
                command_dict['duration_s'] = 10
            command_dict['mode'] = command_arguments['command'].value
            command_dict['userPosition'] = {}
            if vehicle.position is None or vehicle.position.latitude is None or vehicle.position.longitude is None \
                    or vehicle.position.latitude.value is None or vehicle.position.longitude.value is None \
                    or not vehicle.position.latitude.enabled or not vehicle.position.longitude.enabled:
                raise CommandError('Can only execute honk and flash commands if vehicle position is known')
            command_dict['userPosition']['latitude'] = vehicle.position.latitude.value
            command_dict['userPosition']['longitude'] = vehicle.position.longitude.value

            url = f'https://emea.bff.cariad.digital/vehicle/v1/vehicles/{vin}/honkandflash'
            command_response: requests.Response = self.session.post(url, data=json.dumps(command_dict), allow_redirects=True)
            if command_response.status_code not in (requests.codes['ok'], requests.codes['no_content']):
                LOG.error('Could not execute honk or flash command (%s: %s)', command_response.status_code, command_response.text)
                raise CommandError(f'Could not execute honk or flash command ({command_response.status_code}: {command_response.text})')
        else:
            raise CommandError(f'Unknown command {command_arguments["command"]}')
        return command_arguments

    def __on_lock_unlock(self, lock_unlock_command: LockUnlockCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if lock_unlock_command.parent is None or lock_unlock_command.parent.parent is None \
                or lock_unlock_command.parent.parent.parent is None or not isinstance(lock_unlock_command.parent.parent.parent, GenericVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise SetterError('Command arguments are not a dictionary')
        vehicle: GenericVehicle = lock_unlock_command.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        command_dict = {}
        if 'spin' in command_arguments:
            command_dict['spin'] = command_arguments['spin']
        else:
            if self._spin is None:
                raise CommandError('S-PIN is missing, please add S-PIN to your configuration or .netrc file')
            command_dict['spin'] = self._spin
        if command_arguments['command'] == LockUnlockCommand.Command.LOCK:
            url = f'https://emea.bff.cariad.digital/vehicle/v1/vehicles/{vin}/access/lock'
        elif command_arguments['command'] == LockUnlockCommand.Command.UNLOCK:
            url = f'https://emea.bff.cariad.digital/vehicle/v1/vehicles/{vin}/access/unlock'
        else:
            raise CommandError(f'Unknown command {command_arguments["command"]}')
        command_response: requests.Response = self.session.post(url, data=json.dumps(command_dict), allow_redirects=True)
        if command_response.status_code != requests.codes['ok']:
            LOG.error('Could not execute locking command (%s: %s)', command_response.status_code, command_response.text)
            raise CommandError(f'Could not execute locking command ({command_response.status_code}: {command_response.text})')
        return command_arguments

    def __on_spin(self, spin_command: SpinCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        del spin_command
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        command_dict = {}
        if self._spin is None:
            raise CommandError('S-PIN is missing, please add S-PIN to your configuration or .netrc file')
        if 'spin' in command_arguments:
            command_dict['spin'] = command_arguments['spin']
        else:
            if self._spin is None or self._spin == '':
                raise CommandError('S-PIN is missing, please add S-PIN to your configuration or .netrc file')
            command_dict['spin'] = self._spin
        if command_arguments['command'] == SpinCommand.Command.VERIFY:
            url = 'https://emea.bff.cariad.digital/vehicle/v1/spin/verify'
        else:
            raise CommandError(f'Unknown command {command_arguments["command"]}')
        command_response: requests.Response = self.session.post(url, data=json.dumps(command_dict), allow_redirects=True)
        if command_response.status_code != requests.codes['no_content']:
            LOG.error('Could not execute spin command (%s: %s)', command_response.status_code, command_response.text)
            raise CommandError(f'Could not execute spin command ({command_response.status_code}: {command_response.text})')
        else:
            LOG.info('Spin verify command executed successfully')
        return command_arguments

    def __on_charging_start_stop(self, start_stop_command: ChargingStartStopCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if start_stop_command.parent is None or start_stop_command.parent.parent is None \
                or start_stop_command.parent.parent.parent is None or not isinstance(start_stop_command.parent.parent.parent, VolkswagenVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        vehicle: VolkswagenVehicle = start_stop_command.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        if command_arguments['command'] == ChargingStartStopCommand.Command.START:
            url = f'https://emea.bff.cariad.digital/vehicle/v1/vehicles/{vin}/charging/start'
            command_response: requests.Response = self.session.post(url, data='{}', allow_redirects=True)
        elif command_arguments['command'] == ChargingStartStopCommand.Command.STOP:
            url = f'https://emea.bff.cariad.digital/vehicle/v1/vehicles/{vin}/charging/stop'
            command_response: requests.Response = self.session.post(url, data='{}', allow_redirects=True)
        else:
            raise CommandError(f'Unknown command {command_arguments["command"]}')

        if command_response.status_code != requests.codes['ok']:
            LOG.error('Could not start/stop charging (%s: %s)', command_response.status_code, command_response.text)
            raise CommandError(f'Could not start/stop charging ({command_response.status_code}: {command_response.text})')
        return command_arguments
