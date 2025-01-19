"""Module implements the connector to interact with the Skoda API."""
from __future__ import annotations
from typing import TYPE_CHECKING

import threading

import os
import logging
import netrc
from datetime import datetime, timezone, timedelta
import requests

from carconnectivity.garage import Garage
from carconnectivity.errors import AuthenticationError, TooManyRequestsError, RetrievalError, APIError, APICompatibilityError, \
    TemporaryAuthenticationError, ConfigurationError
from carconnectivity.util import robust_time_parse, log_extra_keys, config_remove_credentials
from carconnectivity.units import Length
from carconnectivity.vehicle import GenericVehicle
from carconnectivity.doors import Doors
from carconnectivity.windows import Windows
from carconnectivity.lights import Lights
from carconnectivity.drive import GenericDrive, ElectricDrive, CombustionDrive
from carconnectivity.attributes import BooleanAttribute, DurationAttribute
from carconnectivity_connectors.base.connector import BaseConnector
from carconnectivity_connectors.volkswagen.auth.session_manager import SessionManager, SessionUser, Service
from carconnectivity_connectors.volkswagen.auth.we_connect_session import WeConnectSession
from carconnectivity_connectors.volkswagen.vehicle import VolkswagenVehicle, VolkswagenElectricVehicle, VolkswagenCombustionVehicle, \
    VolkswagenHybridVehicle
from carconnectivity_connectors.volkswagen.capability import Capability
from carconnectivity_connectors.volkswagen._version import __version__


if TYPE_CHECKING:
    from typing import Dict, List, Optional, Any

    from carconnectivity.carconnectivity import CarConnectivity

LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.volkswagen")
LOG_API: logging.Logger = logging.getLogger("carconnectivity.connectors.volkswagen-api-debug")


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
                username, _, password = secret
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
        self._session: WeConnectSession = session
        self._session.retries = 3
        self._session.timeout = 180
        self._session.refresh()

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
        self._session.close()
        BaseConnector.shutdown(self)

    def fetch_all(self) -> None:
        """
        Fetches all necessary data for the connector.

        This method calls the `fetch_vehicles` method to retrieve vehicle data.
        """
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
        data: Dict[str, Any] | None = self._fetch_data(url, session=self._session)

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

                        self.fetch_vehicle_status(vehicle)
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
        data: Dict[str, Any] | None = self._fetch_data(url, self._session)
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
                        log_extra_keys(LOG_API, 'fuelLevelStatus', data['measurements']['fuelLevelStatus'], {'carCapturedTimestamp', 'carType'})
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
            log_extra_keys(LOG_API, 'selectivestatus', data, {'measurements', 'access', 'vehicleLights'})

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
