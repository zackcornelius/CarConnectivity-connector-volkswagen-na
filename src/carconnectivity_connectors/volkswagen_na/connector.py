"""Module implements the connector to interact with the Volskwagen API."""

from __future__ import annotations
from typing import TYPE_CHECKING

import threading

import json
import os
import traceback
import logging
import netrc
from datetime import datetime, timezone, timedelta
import hashlib
import re
import requests
import jwt

from requests.exceptions import HTTPError
from carconnectivity.garage import Garage
from carconnectivity.errors import (
    AuthenticationError,
    TooManyRequestsError,
    RetrievalError,
    APIError,
    APICompatibilityError,
    TemporaryAuthenticationError,
    SetterError,
    CommandError,
)
from carconnectivity.util import robust_time_parse, log_extra_keys, config_remove_credentials
from carconnectivity.units import Length, Power, Speed, GenericUnit
from carconnectivity.vehicle import GenericVehicle
from carconnectivity.doors import Doors
from carconnectivity.windows import Windows
from carconnectivity.lights import Lights
from carconnectivity.drive import GenericDrive, ElectricDrive, CombustionDrive, DieselDrive
from carconnectivity.attributes import (
    BooleanAttribute,
    DurationAttribute,
    GenericAttribute,
    TemperatureAttribute,
    DateAttribute,
    EnumAttribute,
    FloatAttribute,
    LevelAttribute,
    CurrentAttribute,
)
from carconnectivity.units import Temperature
from carconnectivity.command_impl import (
    ClimatizationStartStopCommand,
    HonkAndFlashCommand,
    LockUnlockCommand,
    ChargingStartStopCommand,
    WindowHeatingStartStopCommand,
)
from carconnectivity.climatization import Climatization
from carconnectivity.commands import Commands
from carconnectivity.charging import Charging
from carconnectivity.charging_connector import ChargingConnector
from carconnectivity.position import Position
from carconnectivity.enums import ConnectionState
from carconnectivity.window_heating import WindowHeatings

from carconnectivity_connectors.base.connector import BaseConnector
from carconnectivity_connectors.volkswagen_na.auth.session_manager import SessionManager, SessionUser, Service
from carconnectivity_connectors.volkswagen_na.auth.myvw_session import MyVWSession
from carconnectivity_connectors.volkswagen_na.auth.openid_session import AccessType
from carconnectivity_connectors.volkswagen_na.vehicle import VolkswagenNAVehicle, VolkswagenNAElectricVehicle, VolkswagenNACombustionVehicle
from carconnectivity_connectors.volkswagen_na.climatization import VolkswagenClimatization
from carconnectivity_connectors.volkswagen_na.capability import Capability
from carconnectivity_connectors.volkswagen_na._version import __version__
from carconnectivity_connectors.volkswagen_na.charging import VolkswagenNACharging, mapping_volskwagen_charging_state

SUPPORT_IMAGES = False
try:
    from PIL import Image
    import base64
    import io

    SUPPORT_IMAGES = True
    from carconnectivity.attributes import ImageAttribute
except ImportError:
    pass

if TYPE_CHECKING:
    from typing import Dict, List, Optional, Any, Union, TypeVar

    UnitVar = TypeVar("UnitVar", bound=GenericUnit)

    from carconnectivity.carconnectivity import CarConnectivity

LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.volkswagen_na")
LOG_API: logging.Logger = logging.getLogger("carconnectivity.connectors.volkswagen_na-api-debug")


def _get_http_status_code(err: HTTPError) -> int | None:
    """Extract HTTP status code from an HTTPError, with fallback to parsing the error message."""
    if hasattr(err, "response") and err.response is not None and hasattr(err.response, "status_code"):
        return err.response.status_code
    # Fallback: parse from error message like "403 Client Error: ..."
    match = re.match(r"^(\d{3})\s", str(err))
    if match:
        return int(match.group(1))
    return None


# pylint: disable=too-many-lines
class Connector(BaseConnector):
    """
    Connector class for Volkswagen API connectivity.
    Args:
        car_connectivity (CarConnectivity): An instance of CarConnectivity.
        config (Dict): Configuration dictionary containing connection details.
        initialization (Optional[Dict]): Optional dictionary for initialization parameters.
    Attributes:
        max_age (Optional[int]): Maximum age for cached data in seconds.
    """

    def __init__(self, connector_id: str, car_connectivity: CarConnectivity, config: Dict, *args, initialization: Optional[Dict] = None, **kwargs) -> None:
        BaseConnector.__init__(
            self,
            connector_id=connector_id,
            car_connectivity=car_connectivity,
            config=config,
            log=LOG,
            api_log=LOG_API,
            *args,
            initialization=initialization,
            **kwargs,
        )

        self._background_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self.connection_state: EnumAttribute = EnumAttribute(
            name="connection_state", parent=self, value_type=ConnectionState, value=ConnectionState.DISCONNECTED, tags={"connector_custom"}
        )
        self.interval: DurationAttribute = DurationAttribute(name="interval", parent=self, tags={"connector_custom"})
        self.interval.minimum = timedelta(seconds=180)
        self.interval._is_changeable = True  # pylint: disable=protected-access

        self.commands: Commands = Commands(parent=self)

        LOG.info("Loading volkswagen connector with config %s", config_remove_credentials(config))

        if "spin" in config and config["spin"] is not None:
            self.active_config["spin"] = config["spin"]
        else:
            self.active_config["spin"] = None

        self.active_config["set_spin"] = False
        if "set_spin" in config and config["set_spin"] is not None:
            self.active_config["set_spin"] = config["set_spin"]

        self.active_config["username"] = None
        self.active_config["password"] = None
        if "username" in config and "password" in config:
            self.active_config["username"] = config["username"]
            self.active_config["password"] = config["password"]
        else:
            if "netrc" in config:
                self.active_config["netrc"] = config["netrc"]
            else:
                self.active_config["netrc"] = os.path.join(os.path.expanduser("~"), ".netrc")
            try:
                secrets = netrc.netrc(file=self.active_config["netrc"])
                secret: tuple[str, str, str] | None = secrets.authenticators("volkswagen_na")
                if secret is None:
                    raise AuthenticationError(f"Authentication using {self.active_config['netrc']} failed: volkswagen not found in netrc")
                self.active_config["username"], account, self.active_config["password"] = secret

                if self.active_config["spin"] is None and account is not None:
                    try:
                        self.active_config["spin"] = account
                    except ValueError as err:
                        LOG.error("Could not parse spin from netrc: %s", err)
            except netrc.NetrcParseError as err:
                LOG.error("Authentification using %s failed: %s", self.active_config["netrc"], err)
                raise AuthenticationError(f"Authentication using {self.active_config['netrc']} failed: {err}") from err
            except TypeError as err:
                if "username" not in config:
                    raise AuthenticationError(
                        f'"volkswagen" entry was not found in {self.active_config["netrc"]} netrc-file. Create it or provide username and password in config'
                    ) from err
            except FileNotFoundError as err:
                raise AuthenticationError(
                    f"{self.active_config['netrc']} netrc-file was not found. Create it or provide username and password in config"
                ) from err

        self.active_config["interval"] = 300
        if "interval" in config:
            self.active_config["interval"] = config["interval"]
            if self.active_config["interval"] < 180:
                raise ValueError("Intervall must be at least 180 seconds")
        self.active_config["max_age"] = self.active_config["interval"] - 1
        if "max_age" in config:
            self.active_config["max_age"] = config["max_age"]
        self.interval._set_value(timedelta(seconds=self.active_config["interval"]))  # pylint: disable=protected-access

        if self.active_config["username"] is None or self.active_config["password"] is None:
            raise AuthenticationError("Username or password not provided")

        self._manager: SessionManager = SessionManager(tokenstore=car_connectivity.get_tokenstore(), cache=car_connectivity.get_cache())
        service = Service.MY_VW
        if "country" in config and config["country"] is not None and config["country"].lower() == "ca":
            service = Service.MY_VW_CA
        session: requests.Session = self._manager.get_session(
            service, SessionUser(username=self.active_config["username"], password=self.active_config["password"])
        )
        if not isinstance(session, MyVWSession):
            raise AuthenticationError("Could not create session")
        self.session: MyVWSession = session
        countrypart = "us"
        if "country" in config:
            countrypart = config["country"]
        self.base_url = f"https://b-h-s.spr.{countrypart}00.p.con-veh.net"
        self.session.retries = 3
        self.session.timeout = 180

        self._elapsed: List[timedelta] = []

    def startup(self) -> None:
        self._background_thread = threading.Thread(target=self._background_loop, daemon=False)
        self._background_thread.name = "carconnectivity.connectors.volkswagen-background"
        self._background_thread.start()
        self.healthy._set_value(value=True)  # pylint: disable=protected-access

    def _background_loop(self) -> None:
        self._stop_event.clear()
        fetch: bool = True
        self.connection_state._set_value(value=ConnectionState.CONNECTING)  # pylint: disable=protected-access
        while not self._stop_event.is_set():
            interval = 300
            try:
                try:
                    if fetch:
                        self.fetch_all()
                        fetch = False
                    else:
                        self.update_vehicles()
                    self.last_update._set_value(value=datetime.now(tz=timezone.utc))  # pylint: disable=protected-access
                    if self.interval.value is not None:
                        interval: float = self.interval.value.total_seconds()
                except Exception:
                    self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                    if self.interval.value is not None:
                        interval: float = self.interval.value.total_seconds()
                    raise
            except TooManyRequestsError as err:
                LOG.error("Retrieval error during update. Too many requests from your account (%s). Will try again after 15 minutes", str(err))
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(900)
            except RetrievalError as err:
                LOG.error("Retrieval error during update (%s). Will try again after configured interval of %ss", str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
            except APICompatibilityError as err:
                LOG.error("API compatability error during update (%s). Will try again after configured interval of %ss", str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
            except TemporaryAuthenticationError as err:
                LOG.error("Temporary authentification error during update (%s). Will try again after configured interval of %ss", str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
            except Exception as err:
                LOG.critical("Critical error during update: %s", traceback.format_exc())
                self.healthy._set_value(value=False)  # pylint: disable=protected-access
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                raise err
            else:
                self.connection_state._set_value(value=ConnectionState.CONNECTED)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
        # When leaving the loop, set the connection state to disconnected
        self.connection_state._set_value(value=ConnectionState.DISCONNECTED)  # pylint: disable=protected-access

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
        self.session.close()
        if self._background_thread is not None:
            self._background_thread.join()
        self.persist()
        BaseConnector.shutdown(self)

    def fetch_all(self) -> None:
        """
        Fetches all necessary data for the connector.

        This method calls the `fetch_vehicles` method to retrieve vehicle data.
        """
        self.fetch_vehicles()

    def update_vehicles(self) -> None:
        """
        Updates the status of all vehicles in the garage managed by this connector.

        This method iterates through all vehicle VINs in the garage, and for each vehicle that is
        managed by this connector and is an instance of VolkswagenNAVehicle, it updates the vehicle's status
        by fetching data from various APIs. If the vehicle is an instance of VolkswagenNAElectricVehicle,
        it also fetches charging information.

        Returns:
            None
        """
        garage: Garage = self.car_connectivity.garage
        for vin in set(garage.list_vehicle_vins()):
            vehicle_to_update: Optional[GenericVehicle] = garage.get_vehicle(vin)
            if vehicle_to_update is not None and isinstance(vehicle_to_update, VolkswagenNAVehicle) and vehicle_to_update.is_managed_by_connector(self):
                self.fetch_vehicle_status(vehicle_to_update)

                self.decide_state(vehicle_to_update)
        self.car_connectivity.transaction_end()

    def update_boolean(self, attribute: BooleanAttribute, value: Optional[bool], captured_at: Optional[datetime] = None) -> None:
        """
        Updates a BooleanAttribute with a new value and logs the update.

        Args:
            attribute (BooleanAttribute): The BooleanAttribute to be updated.
            value (Optional[bool]): The new value to set for the attribute.
            captured_at (Optional[datetime]): The timestamp when the value was captured. If None, the current time is used.

        Returns:
            None
        """
        if captured_at is None:
            captured_at = datetime.now(tz=timezone.utc)
        if attribute.value != value:
            attribute.last_updated = captured_at - timedelta(
                seconds=1
            )  # To ensure that the state gets updated even if only the status changes but not the timestamp
            attribute._set_value(value=value, measured=captured_at)  # pylint: disable=protected-access

    def update_float(
        self, attribute: FloatAttribute[UnitVar], value: Optional[float], captured_at: Optional[datetime] = None, unit: Optional[UnitVar] = None
    ) -> None:
        """
        Updates a FloatAttribute with a new value and logs the update.

        Args:
            attribute (FloatAttribute): The FloatAttribute to be updated.
            value (Optional[float]): The new value to set for the attribute.
            captured_at (Optional[datetime]): The timestamp when the value was captured. If None, the current time is used.

        Returns:
            None
        """
        if captured_at is None:
            captured_at = datetime.now(tz=timezone.utc)
        if attribute.value != value:
            attribute.last_updated = captured_at - timedelta(
                seconds=1
            )  # To ensure that the state gets updated even if only the status changes but not the timestamp
            attribute._set_value(value=value, measured=captured_at, unit=unit)  # pylint: disable=protected-access

    def update_datetime(self, attribute: DateAttribute, value: Optional[datetime], captured_at: Optional[datetime] = None) -> None:
        """
        Updates a DateAttribute with a new value and logs the update.

        Args:
            attribute (DateAttribute): The DateAttribute to be updated.
            value (Optional[datetime]): The new value to set for the attribute.
            captured_at (Optional[datetime]): The timestamp when the value was captured. If None, the current time is used.

        Returns:
            None
        """
        if captured_at is None:
            captured_at = datetime.now(tz=timezone.utc)
        if attribute.value != value:
            attribute.last_updated = captured_at - timedelta(
                seconds=1
            )  # To ensure that the state gets updated even if only the status changes but not the timestamp
            attribute._set_value(value=value, measured=captured_at)  # pylint: disable=protected-access

    def update_enum(self, attribute: EnumAttribute, value: Any, captured_at: Optional[datetime] = None) -> None:
        """
        Updates an EnumAttribute with a new value and logs the update.

        Args:
            attribute (EnumAttribute[T]): The EnumAttribute to be updated.
            value (Optional[T]): The new value to set for the attribute.
            captured_at (Optional[datetime]): The timestamp when the value was captured. If None, the current time is used.

        Returns:
            None
        """
        if captured_at is None:
            captured_at = datetime.now(tz=timezone.utc)
        if attribute.value != value:
            attribute.last_updated = captured_at - timedelta(
                seconds=1
            )  # To ensure that the state gets updated even if only the status changes but not the timestamp
            attribute._set_value(value=value, measured=captured_at)  # pylint: disable=protected-access

    def fetch_vehicles(self) -> None:
        """
        Fetches the list of vehicles from the Volkswagen Connect API and updates the garage with new vehicles.
        This method sends a request to the Volkswagen Connect API to retrieve the list of vehicles associated with the user's account.
        If new vehicles are found in the response, they are added to the garage.

        Returns:
            None
        """
        garage: Garage = self.car_connectivity.garage
        url = self.base_url + "/account/v1/garage"
        data: Dict[str, Any] | None = self._fetch_data(url, session=self.session)

        seen_vehicle_vins: set[str] = set()
        if data is not None:
            if "data" in data and data["data"] is not None:
                for vehicle_dict in data["data"]["vehicles"]:
                    if "vin" in vehicle_dict and vehicle_dict["vin"] is not None:
                        if vehicle_dict["vin"] in self.active_config["hide_vins"]:
                            LOG.info("Vehicle %s filtered out due to configuration", vehicle_dict["vin"])
                            continue
                        seen_vehicle_vins.add(vehicle_dict["vin"])
                        vehicle: Optional[VolkswagenNAVehicle] = garage.get_vehicle(vehicle_dict["vin"])  # pyright: ignore[reportAssignmentType]
                        if vehicle is None:
                            vehicle = VolkswagenNAVehicle(
                                vin=vehicle_dict["vin"], garage=garage, managing_connector=self, initialization=garage.get_initialization(vehicle_dict["vin"])
                            )
                            garage.add_vehicle(vehicle_dict["vin"], vehicle)

                        if "vehicleId" not in vehicle_dict or vehicle_dict["vehicleId"] is None:
                            raise ValueError("Cannot have VW NA vehicle without vehicleId")

                        # pylint: disable=protected-access
                        vehicle.uuid._set_value(value=vehicle_dict["vehicleId"])

                        if "vehicleNickName" in vehicle_dict and vehicle_dict["vehicleNickName"] is not None:
                            vehicle.name._set_value(vehicle_dict["vehicleNickName"])  # pylint: disable=protected-access
                        else:
                            vehicle.name._set_value(None)  # pylint: disable=protected-access

                        if "modelName" in vehicle_dict and vehicle_dict["modelName"] is not None:
                            vehicle.model._set_value(vehicle_dict["modelName"])  # pylint: disable=protected-access
                        else:
                            vehicle.model._set_value(None)  # pylint: disable=protected-access

                        rrs_url = self.base_url + f"/rrs/v1/privileges/user/{self.session.backend_user_id}/vehicle/{vehicle.uuid.value}"
                        try:
                            rrs_response = self.session.get(rrs_url)
                            rrs_data = rrs_response.json()
                            # rrs_data = self._fetch_data(rrs_url, session=self.session)
                        except HTTPError as err:
                            LOG.error("Error fetching RRS data for vehicle %s: %s", vehicle.vin, str(err))
                            rrs_data = None

                        if (
                            rrs_data
                            and "data" in rrs_data
                            and rrs_data["data"] is not None
                            and "services" in rrs_data["data"]
                            and rrs_data["data"]["services"] is not None
                        ):
                            services = rrs_data["data"]["services"]
                            found_capabilities = set()
                            for service_dict in services:
                                if "longCode" in service_dict and service_dict["longCode"] is not None:
                                    service_id = service_dict["longCode"]
                                    for operation in service_dict.get("operations", []):
                                        capability_id = service_id + ":" + operation["longCode"]
                                        found_capabilities.add(capability_id)
                                        if vehicle.capabilities.has_capability(capability_id):
                                            c = vehicle.capabilities.get_capability(capability_id)
                                            if c is None:
                                                raise ValueError(
                                                    f"Capability {capability_id} not found on vehicle {vehicle.vin} although it was found in capabilities list"
                                                )
                                            capability: Capability = c
                                        else:
                                            capability = Capability(
                                                capability_id=capability_id,
                                                capabilities=vehicle.capabilities,
                                                initialization=vehicle.capabilities.get_initialization(capability_id),
                                            )
                                            vehicle.capabilities.add_capability(capability_id, capability)
                                        if "capabilityStatus" in operation and operation["capabilityStatus"] is not None:
                                            status = operation["capabilityStatus"]
                                            if status in [item.value for item in Capability.Status]:
                                                capability.status._set_value(value=[Capability.Status(status)])  # pylint: disable=protected-access
                                            elif status == "AVAILABLE":
                                                pass  # No status
                                            else:
                                                LOG_API.warning("Capability status unkown %s", status)
                                                capability.status._set_value(value=[Capability.Status.UNKNOWN])  # pylint: disable=protected-access

                                        log_extra_keys(
                                            LOG_API,
                                            "service.operation",
                                            operation,
                                            {"longCode", "shortCode", "capabilityStatus", "subscriptionStatus", "privilege", "playProtection"},
                                        )
                            for capability_id in vehicle.capabilities.capabilities.keys() - found_capabilities:
                                vehicle.capabilities.remove_capability(capability_id)
                        else:
                            vehicle.capabilities.clear_capabilities()

                        # Add honkAndFlash command if necessary capabilities are available
                        has_honk_and_flash: bool = vehicle.capabilities.has_capability("HonkAndFlash:ALL", check_status_ok=True)
                        if has_honk_and_flash:
                            if vehicle.commands is not None and vehicle.commands.commands is not None and not vehicle.commands.contains_command("honk-flash"):
                                honk_flash_command = HonkAndFlashCommand(parent=vehicle.commands, with_duration=True)
                                honk_flash_command._add_on_set_hook(self.__on_honk_flash)  # pylint: disable=protected-access
                                honk_flash_command.enabled = True
                                vehicle.commands.add_command(honk_flash_command)

                        # Add lock and unlock command
                        has_capability_access = vehicle.capabilities.has_capability("LockAndUnlock:ALL", check_status_ok=True)
                        LOG.debug("Vehicle %s has lock and unlock capability: %s", vehicle.vin, has_capability_access)
                        if has_capability_access:
                            if (
                                vehicle.doors is not None
                                and vehicle.doors.commands is not None
                                and vehicle.doors.commands.commands is not None
                                and not vehicle.doors.commands.contains_command("lock-unlock")
                            ):
                                lock_unlock_command = LockUnlockCommand(parent=vehicle.doors.commands)
                                lock_unlock_command._add_on_set_hook(self.__on_lock_unlock)  # pylint: disable=protected-access
                                lock_unlock_command.enabled = True
                                vehicle.doors.commands.add_command(lock_unlock_command)

                        if SUPPORT_IMAGES:
                            # fetch vehcile images
                            if "representativeImgURLComplete" in vehicle_dict:
                                imageurl: str = vehicle_dict["representativeImgURLComplete"]
                                img = None
                                cache_date = None
                                if self.active_config["max_age"] is not None and self.session.cache is not None and imageurl in self.session.cache:
                                    img, cache_date_string = self.session.cache[imageurl]
                                    img = base64.b64decode(img)  # pyright: ignore[reportPossiblyUnboundVariable]
                                    img = Image.open(io.BytesIO(img))  # pyright: ignore[reportPossiblyUnboundVariable]
                                    cache_date = datetime.fromisoformat(cache_date_string)
                                if (
                                    img is None
                                    or self.active_config["max_age"] is None
                                    or (cache_date is not None and cache_date < (datetime.utcnow() - timedelta(seconds=self.active_config["max_age"])))
                                ):
                                    try:
                                        image_download_response = self.session.get(imageurl, stream=True)
                                        if image_download_response.status_code == requests.codes["ok"]:
                                            img = Image.open(image_download_response.raw)  # pyright: ignore[reportPossiblyUnboundVariable]
                                            if self.session.cache is not None:
                                                buffered = io.BytesIO()  # pyright: ignore[reportPossiblyUnboundVariable]
                                                img.save(buffered, format="PNG")
                                                img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")  # pyright: ignore[reportPossiblyUnboundVariable]
                                                self.session.cache[imageurl] = (img_str, str(datetime.utcnow()))
                                        elif image_download_response.status_code == requests.codes["unauthorized"]:
                                            LOG.info("Server asks for new authorization")
                                            self.session.login()
                                            image_download_response = self.session.get(imageurl, stream=True)
                                            if image_download_response.status_code == requests.codes["ok"]:
                                                img = Image.open(image_download_response.raw)  # pyright: ignore[reportPossiblyUnboundVariable]
                                                if self.session.cache is not None:
                                                    buffered = io.BytesIO()  # pyright: ignore[reportPossiblyUnboundVariable]
                                                    img.save(buffered, format="PNG")
                                                    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")  # pyright: ignore[reportPossiblyUnboundVariable]
                                                    self.session.cache[imageurl] = (img_str, str(datetime.utcnow()))
                                    except requests.exceptions.ConnectionError as connection_error:
                                        raise RetrievalError(f"Connection error: {connection_error}") from connection_error
                                    except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
                                        raise RetrievalError(f"Error: {chunked_encoding_error}") from chunked_encoding_error
                                    except requests.exceptions.ReadTimeout as timeout_error:
                                        raise RetrievalError(f"Timeout during read: {timeout_error}") from timeout_error
                                    except requests.exceptions.RetryError as retry_error:
                                        raise RetrievalError(f"Retrying failed: {retry_error}") from retry_error
                                if img is not None:
                                    vehicle._car_images["represntative"] = img  # pylint: disable=protected-access
                                    if "car_picture" in vehicle.images.images:
                                        vehicle.images.images["car_picture"]._set_value(img)  # pylint: disable=protected-access
                                    else:
                                        vehicle.images.images["car_picture"] = ImageAttribute(
                                            name="car_picture", parent=vehicle.images, value=img, tags={"carconnectivity"}
                                        )
                    else:
                        raise APIError("Could not fetch vehicle data, VIN missing")
        for vin in set(garage.list_vehicle_vins()) - seen_vehicle_vins:
            vehicle_to_remove = garage.get_vehicle(vin)
            if vehicle_to_remove is not None and vehicle_to_remove.is_managed_by_connector(self):
                garage.remove_vehicle(vin)
        self.update_vehicles()

    def decide_state(self, vehicle: VolkswagenNAVehicle) -> None:
        """
        Decides the state of the vehicle based on the current data.

        Args:
            vehicle (VolkswagenNAVehicle): The Volkswagen vehicle object.
        """
        if vehicle is not None:
            # pylint: disable=protected-access
            if (
                vehicle.connection_state is not None
                and vehicle.connection_state.enabled
                and vehicle.connection_state.value == GenericVehicle.ConnectionState.OFFLINE
            ):
                vehicle.state._set_value(GenericVehicle.State.OFFLINE)
            elif vehicle.is_active is not None and vehicle.is_active.enabled and vehicle.is_active.value:
                vehicle.state._set_value(GenericVehicle.State.IGNITION_ON)  # pylint: disable=protected-access
            elif (
                vehicle.position is not None
                and vehicle.position.enabled
                and vehicle.position.position_type is not None
                and vehicle.position.position_type.enabled
                and vehicle.position.position_type.value == Position.PositionType.PARKING
            ):
                vehicle.state._set_value(GenericVehicle.State.PARKED)  # pylint: disable=protected-access
            else:
                vehicle.state._set_value(GenericVehicle.State.UNKNOWN)  # pylint: disable=protected-access

    def fetch_vehicle_status(self, vehicle: VolkswagenNAVehicle) -> None:
        """
        Fetches the status of a vehicle from the Volkswagen API.

        Args:
            vehicle (GenericVehicle): The vehicle object containing the VIN.

        Returns:
            None
        """
        # pylint: disable=protected-access
        vin = vehicle.vin.value
        if vin is None:
            raise ValueError("vehicle.vin cannot be None")

        try:
            token = self.__do_spin(vehicle)
        except (HTTPError, AuthenticationError, RetrievalError) as err:
            LOG.error("Authentication error during fetching spin token (type=%s): %s", type(err).__name__, str(err))
            token = None

        url = self.base_url + f"/rvs/v1/vehicle/{vehicle.uuid}"
        data = None

        try:
            data: Dict[str, Any] | None = self._fetch_data(url, self.session, token=token)
        except RetrievalError as err:
            LOG.error("Error fetching vehicle status for vin %s (type=%s): %s", vehicle.vin, type(err).__name__, str(err))
            # Check if this is actually a wrapped 403 error
            err_str = str(err)
            if "403" in err_str or "forbidden" in err_str.lower():
                LOG.warning("Got 403 (wrapped as %s) fetching vehicle status for vin %s, refreshing auth and retrying", type(err).__name__, vehicle.vin)
                try:
                    self.session.refresh()
                except (AuthenticationError, Exception):
                    try:
                        self.session.login()
                    except Exception as login_err:
                        LOG.error("Re-login failed during 403 recovery: %s", str(login_err))
                        data = None
                # Invalidate cached SPIN token so we get a fresh one
                vehicle.spin_token = None
                vehicle.spin_token_expiry = None
                try:
                    token = self.__do_spin(vehicle)
                except (HTTPError, AuthenticationError):
                    token = None
                try:
                    data = self._fetch_data(url, self.session, token=token)
                except (HTTPError, RetrievalError) as retry_err:
                    LOG.error("Retry after 403 also failed for vin %s: %s", vehicle.vin, str(retry_err))
                    data = None
        except HTTPError as err:
            http_status = _get_http_status_code(err)
            LOG.error(
                "HTTPError in fetch_vehicle_status (type=%s): status_code=%s, response=%s, err=%s", type(err).__name__, http_status, err.response, str(err)
            )
            if http_status == 403:
                LOG.warning("Got 403 fetching vehicle status for vin %s, refreshing auth and retrying", vehicle.vin)
                try:
                    self.session.refresh()
                except (AuthenticationError, Exception):
                    try:
                        self.session.login()
                    except Exception as login_err:
                        LOG.error("Re-login failed during 403 recovery: %s", str(login_err))
                        data = None
                # Invalidate cached SPIN token so we get a fresh one
                vehicle.spin_token = None
                vehicle.spin_token_expiry = None
                try:
                    token = self.__do_spin(vehicle)
                except (HTTPError, AuthenticationError):
                    token = None
                try:
                    data = self._fetch_data(url, self.session, token=token)
                except (HTTPError, RetrievalError) as retry_err:
                    LOG.error("Retry after 403 also failed for vin %s: %s", vehicle.vin, str(retry_err))
                    data = None
            else:
                LOG.error("Error fetching vehicle status for vin %s: %s", vehicle.vin, str(err))
        if data is not None and "data" in data:
            data = data["data"]
        if data is not None:
            if "timestamp" in data and data["timestamp"] is not None:
                captured_at: datetime = datetime.fromtimestamp(data["timestamp"] / 1000, tz=timezone.utc)
            elif "clampStateTimestamp" in data and data["clampStateTimestamp"] is not None:
                captured_at: datetime = datetime.fromtimestamp(data["clampStateTimestamp"] / 1000, tz=timezone.utc)
            elif "instrumentCluserTime" in data and data["instrumentCluserTime"] is not None:
                captured_at: datetime = robust_time_parse(data["instrumentCluserTime"])
            else:
                raise APIError("Could not fetch vehicle status, timestamp, clampStateTimestamp and instrumentCluserTime missing")

            if "platform" in data and data["platform"] in ["MEB"]:
                if not isinstance(vehicle, VolkswagenNAElectricVehicle):
                    LOG.debug("Promoting %s to VolkswagenNAElectricVehicle object for %s", vehicle.__class__.__name__, vin)
                    vehicle = VolkswagenNAElectricVehicle(garage=self.car_connectivity.garage, origin=vehicle)
                    self.car_connectivity.garage.replace_vehicle(vin, vehicle)
            elif not isinstance(vehicle, VolkswagenNACombustionVehicle):
                # No hybrids in the US right now
                LOG.debug("Promoting %s to VolkswagenNACombustionVehicle object for %s", vehicle.__class__.__name__, vin)
                vehicle = VolkswagenNACombustionVehicle(garage=self.car_connectivity.garage, origin=vehicle)
                self.car_connectivity.garage.replace_vehicle(vin, vehicle)
            if "powerStatus" in data and data["powerStatus"] is not None:
                power_status = data["powerStatus"]

                drive_id = "primary"
                if isinstance(vehicle, VolkswagenNAElectricVehicle):
                    engine_type: GenericDrive.Type = GenericDrive.Type.ELECTRIC
                else:
                    engine_type: GenericDrive.Type = GenericDrive.Type.GASOLINE

                if drive_id in vehicle.drives.drives:
                    drive: GenericDrive = vehicle.drives.drives[drive_id]
                else:
                    if engine_type == GenericDrive.Type.ELECTRIC:
                        drive = ElectricDrive(drive_id=drive_id, drives=vehicle.drives, initialization=vehicle.drives.get_initialization(drive_id))
                    elif engine_type == GenericDrive.Type.DIESEL:
                        drive = DieselDrive(drive_id=drive_id, drives=vehicle.drives, initialization=vehicle.drives.get_initialization(drive_id))
                    elif engine_type in [
                        GenericDrive.Type.FUEL,
                        GenericDrive.Type.GASOLINE,
                        GenericDrive.Type.PETROL,
                        GenericDrive.Type.CNG,
                        GenericDrive.Type.LPG,
                    ]:
                        drive = CombustionDrive(drive_id=drive_id, drives=vehicle.drives, initialization=vehicle.drives.get_initialization(drive_id))
                    else:
                        drive = GenericDrive(drive_id=drive_id, drives=vehicle.drives, initialization=vehicle.drives.get_initialization(drive_id))
                    drive.type._set_value(engine_type)  # pylint: disable=protected-access
                    vehicle.drives.add_drive(drive)
                if "cruiseRange" in power_status and power_status["cruiseRange"] is not None:
                    self.update_float(
                        drive.range, float(power_status["cruiseRange"]), captured_at, unit=Length.KM if power_status["cruiseRangeUnits"] == "KM" else Length.MI
                    )
                    drive.range.precision = 1
                    self.update_float(
                        vehicle.drives.total_range,
                        float(power_status["cruiseRange"]),
                        captured_at,
                        unit=Length.KM if power_status["cruiseRangeUnits"] == "KM" else Length.MI,
                    )
                    vehicle.drives.total_range.precision = 1
                else:
                    drive.range._set_value(None, measured=captured_at, unit=Length.KM)  # pylint: disable=protected-access
                    vehicle.drives.total_range._set_value(None)  # pylint: disable=protected-access

                if (
                    "fuelPercentRemaining" in power_status
                    and power_status["fuelPercentRemaining"] is not None
                    and engine_type is not GenericDrive.Type.ELECTRIC
                ):
                    self.update_float(drive.level, float(power_status["fuelPercentRemaining"]), captured_at)
                    drive.level.precision = 1

                log_extra_keys(
                    LOG_API, "powerStatus", power_status, {"cruiseRange", "fuelPercentRemaining", "cruiseRangeUnits", "cruiseRangeFirst", "cruiseRangeSecond"}
                )

            if "currentMileage" in data and data["currentMileage"] is not None:
                LOG.debug("+===== Setting Odometer to %s km", data["currentMileage"])
                # pylint: disable-next=protected-access
                LOG.debug("Captured_at timezone info: %s", str(captured_at.tzinfo))
                LOG.debug(
                    "Last captured timezone info: %s", str(vehicle.odometer.last_updated.tzinfo if vehicle.odometer.last_updated is not None else "Not Present")
                )
                self.update_float(vehicle.odometer, float(data["currentMileage"]), captured_at, unit=Length.KM)
                vehicle.odometer.precision = 1
            else:
                self.update_float(vehicle.odometer, None, captured_at, unit=Length.KM)

            if "location" in data and data["location"] is not None:
                if "timestamp" not in data["location"] or data["location"]["timestamp"] is None:
                    raise APIError("Could not fetch vehicle status, timestmap missing")
                captured_at: datetime = datetime.fromtimestamp((data["location"]["timestamp"] / 1000), tz=timezone.utc)

                if (
                    "latitude" in data["location"]
                    and data["location"]["latitude"] is not None
                    and "longitude" in data["location"]
                    and data["location"]["longitude"] is not None
                ):
                    self.update_float(vehicle.position.latitude, float(data["location"]["latitude"]), captured_at)  # pylint: disable=protected-access
                    vehicle.position.latitude.precision = 0.000001
                    self.update_float(vehicle.position.longitude, float(data["location"]["longitude"]), captured_at)  # pylint: disable=protected-access
                    vehicle.position.longitude.precision = 0.000001
                    self.update_enum(vehicle.position.position_type, Position.PositionType.PARKING, captured_at)
                else:
                    LOG.debug("Unable to find valid location data in response: %s", json.dumps(data))
                    vehicle.position.latitude._set_value(None)  # pylint: disable=protected-access
                    vehicle.position.longitude._set_value(None)  # pylint: disable=protected-access
                    vehicle.position.position_type._set_value(None)  # pylint: disable=protected-access
            elif "lastParkedLocation" in data and data["lastParkedLocation"] is not None:
                if "timestamp" not in data["lastParkedLocation"] or data["lastParkedLocation"]["timestamp"] is None:
                    raise APIError("Could not fetch vehicle status, timestmap missing")
                captured_at: datetime = datetime.fromtimestamp((data["lastParkedLocation"]["timestamp"] / 1000), tz=timezone.utc)

                if (
                    "latitude" in data["lastParkedLocation"]
                    and data["lastParkedLocation"]["latitude"] is not None
                    and "longitude" in data["lastParkedLocation"]
                    and data["lastParkedLocation"]["longitude"] is not None
                ):
                    self.update_float(vehicle.position.latitude, float(data["lastParkedLocation"]["latitude"]), captured_at)  # pylint: disable=protected-access
                    vehicle.position.latitude.precision = 0.000001
                    self.update_float(vehicle.position.longitude, float(data["lastParkedLocation"]["longitude"]), captured_at)  # pylint: disable=protected-access
                    vehicle.position.longitude.precision = 0.000001
                    self.update_enum(vehicle.position.position_type, Position.PositionType.PARKING, captured_at)
                else:
                    LOG.debug("Unable to find valid location data in response: %s", json.dumps(data))
                    vehicle.position.latitude._set_value(None)  # pylint: disable=protected-access
                    vehicle.position.longitude._set_value(None)  # pylint: disable=protected-access
                    vehicle.position.position_type._set_value(None)  # pylint: disable=protected-access
            else:
                vehicle.position.latitude._set_value(None)  # pylint: disable=protected-access
                vehicle.position.longitude._set_value(None)  # pylint: disable=protected-access
                vehicle.position.position_type._set_value(None)  # pylint: disable=protected-access

            if "exteriorStatus" in data and data["exteriorStatus"] is not None:
                exterior_status = data["exteriorStatus"]
                seen_door_ids: set[str] = set()
                if "doorStatus" in exterior_status and exterior_status["doorStatus"] is not None:
                    all_doors_closed = True
                    if "doorStatusTimestmap" in exterior_status["doorStatus"]:
                        captured_at = datetime.fromtimestamp((exterior_status["doorStatus"]["doorStatusTimestamp"] / 1000), tz=timezone.utc)
                    for door_id, door_status in exterior_status["doorStatus"].items():
                        if door_status == "NOTAVAILABLE" or door_id == "doorStatusTimestamp":
                            continue
                        seen_door_ids.add(door_id)
                        if door_id in vehicle.doors.doors:
                            door: Doors.Door = vehicle.doors.doors[door_id]
                        else:
                            door = Doors.Door(door_id=door_id, doors=vehicle.doors, initialization=vehicle.doors.get_initialization(door_id))
                            door.open_state.last_updated = captured_at - timedelta(
                                seconds=1
                            )  # To ensure that the state gets updated even if only the status changes but not the timestamp
                            vehicle.doors.doors[door_id] = door
                        if door_status == "CLOSED":
                            self.update_enum(door.open_state, Doors.OpenState.CLOSED, captured_at)
                        elif door_status == "OPEN":
                            self.update_enum(door.open_state, Doors.OpenState.OPEN, captured_at)
                            all_doors_closed = False
                        else:
                            self.update_enum(door.open_state, Doors.OpenState.UNKNOWN, captured_at)
                            LOG_API.info("Unknown door status %s", door_status)
                    if all_doors_closed:
                        self.update_enum(vehicle.doors.open_state, Doors.OpenState.CLOSED, captured_at)
                    else:
                        self.update_enum(vehicle.doors.open_state, Doors.OpenState.OPEN, captured_at)
                if "doorLockStatus" in exterior_status and exterior_status["doorLockStatus"] is not None:
                    if "doorLockStatusTimestmap" in exterior_status["doorLockStatus"]:
                        captured_at = datetime.fromtimestamp((exterior_status["doorLockStatus"]["doorLockStatusTimestamp"] / 1000), tz=timezone.utc)
                    for door_id, door_status in exterior_status["doorLockStatus"].items():
                        if door_status == "NOTAVAILABLE" or door_id == "doorLockStatusTimestamp":
                            continue
                        seen_door_ids.add(door_id)
                        if door_id in vehicle.doors.doors:
                            door: Doors.Door = vehicle.doors.doors[door_id]
                        else:
                            door = Doors.Door(door_id=door_id, doors=vehicle.doors, initialization=vehicle.doors.get_initialization(door_id))
                            vehicle.doors.doors[door_id] = door
                        if door_status == "LOCKED":
                            self.update_enum(door.lock_state, Doors.LockState.LOCKED, captured_at)
                        elif door_status == "UNLOCKED":
                            self.update_enum(door.lock_state, Doors.LockState.UNLOCKED, captured_at)
                        else:
                            LOG.info("Door %s has unknown lock status: %s", door_id, door_status)
                            self.update_enum(door.lock_state, Doors.LockState.UNKNOWN, captured_at)

                # Disable status of doors that only have some data
                for door_id in seen_door_ids:
                    if door_id not in exterior_status["doorLockStatus"]:
                        vehicle.doors.doors[door_id].lock_state.enabled = False
                    if door_id not in exterior_status["doorStatus"]:
                        vehicle.doors.doors[door_id].open_state.enabled = False

                # Disable doors that are not present in the response at all
                for door_id in vehicle.doors.doors.keys() - seen_door_ids:
                    vehicle.doors.doors[door_id].enabled = False

                # Report full-vehicle state
                if "secure" in exterior_status and exterior_status["secure"] is not None:
                    LOG.debug("Vehicle secure status: %s", exterior_status["secure"])
                    vehicle.doors.lock_state.enabled = True
                    if exterior_status["secure"] == "SECURE":
                        self.update_enum(vehicle.doors.lock_state, Doors.LockState.LOCKED, captured_at)
                    elif exterior_status["secure"] == "UNSECURE":
                        self.update_enum(vehicle.doors.lock_state, Doors.LockState.UNLOCKED, captured_at)
                    else:
                        # Derive overall lock state from individual door lock states when secure is UNKNOWN
                        if "doorLockStatus" in exterior_status and exterior_status["doorLockStatus"] is not None:
                            all_locked = True
                            any_unlocked = False
                            for did, dstatus in exterior_status["doorLockStatus"].items():
                                if did == "doorLockStatusTimestamp" or dstatus == "NOTAVAILABLE":
                                    continue
                                if dstatus == "UNLOCKED":
                                    any_unlocked = True
                                    all_locked = False
                                elif dstatus != "LOCKED":
                                    all_locked = False
                            if any_unlocked:
                                self.update_enum(vehicle.doors.lock_state, Doors.LockState.UNLOCKED, captured_at)
                            elif all_locked:
                                self.update_enum(vehicle.doors.lock_state, Doors.LockState.LOCKED, captured_at)
                            else:
                                self.update_enum(vehicle.doors.lock_state, Doors.LockState.UNKNOWN, captured_at)
                        else:
                            self.update_enum(vehicle.doors.lock_state, Doors.LockState.UNKNOWN, captured_at)
                else:
                    LOG.debug("Vehicle secure status not available")
                    vehicle.doors.lock_state.enabled = False
                    self.update_enum(vehicle.doors.lock_state, None, captured_at)

                if "windowStatus" in exterior_status and exterior_status["windowStatus"] is not None:
                    if "windowStatusTimestmap" in exterior_status["windowStatus"]:
                        captured_at = datetime.fromtimestamp((exterior_status["windowStatus"]["windowStatusTimestamp"] / 1000), tz=timezone.utc)
                    seen_window_ids: set[str] = set()
                    all_windows_closed = True
                    for window_id, window_status in exterior_status["windowStatus"].items():
                        if window_status == "NOTAVAILABLE" or window_id == "windowStatusTimestamp":
                            continue
                        seen_window_ids.add(window_id)
                        if window_id in vehicle.windows.windows:
                            window: Windows.Window = vehicle.windows.windows[window_id]
                        else:
                            window = Windows.Window(window_id=window_id, windows=vehicle.windows, initialization=vehicle.windows.get_initialization(window_id))
                            vehicle.windows.windows[window_id] = window
                        if window_status == "CLOSED":
                            self.update_enum(window.open_state, Windows.OpenState.CLOSED, captured_at)
                        elif window_status == "OPEN":
                            self.update_enum(window.open_state, Windows.OpenState.OPEN, captured_at)
                            all_windows_closed = False
                        elif window_status == "UNSUPPORTED":
                            self.update_enum(window.open_state, Windows.OpenState.UNSUPPORTED, captured_at)
                        elif window_status == "INVALID":
                            self.update_enum(window.open_state, Windows.OpenState.INVALID, captured_at)
                        else:
                            self.update_enum(window.open_state, Windows.OpenState.UNKNOWN, captured_at)
                            LOG_API.info("Unknown window status %s", window_status)
                    if all_windows_closed:
                        self.update_enum(vehicle.windows.open_state, Windows.OpenState.CLOSED, captured_at)
                    else:
                        self.update_enum(vehicle.windows.open_state, Windows.OpenState.OPEN, captured_at)
                    for window_id in vehicle.windows.windows.keys() - seen_window_ids:
                        vehicle.windows.windows[window_id].enabled = False
                else:
                    self.update_enum(vehicle.windows.open_state, None, captured_at)
                    vehicle.windows.enabled = False

                if "lightStatus" in exterior_status and exterior_status["lightStatus"] is not None:
                    all_lights_off = True
                    seen_light_ids: set[str] = set()
                    for light_id, light_status in exterior_status["lightStatus"].items():
                        if light_status == "NOTAVAILABLE":
                            continue
                        seen_light_ids.add(light_id)
                        if light_id in vehicle.lights.lights:
                            light: Lights.Light = vehicle.lights.lights[light_id]
                        else:
                            light: Lights.Light = Lights.Light(
                                light_id=light_id, lights=vehicle.lights, initialization=vehicle.lights.get_initialization(light_id)
                            )
                            vehicle.lights.lights[light_id] = light
                        if light_status == "ON":
                            all_lights_off = False
                            self.update_enum(light.light_state, Lights.LightState.ON, captured_at)
                        elif light_status == "OFF":
                            self.update_enum(light.light_state, Lights.LightState.OFF, captured_at)
                        elif light_status == "INVALID":
                            self.update_enum(light.light_state, Lights.LightState.INVALID, captured_at)
                        else:
                            self.update_enum(light.light_state, Lights.LightState.UNKNOWN, captured_at)
                            LOG_API.info("Unknown light status %s", light_status)
                    if all_lights_off:
                        self.update_enum(vehicle.lights.light_state, Lights.LightState.OFF, captured_at)
                    else:
                        self.update_enum(vehicle.lights.light_state, Lights.LightState.ON, captured_at)
                    for light_id in vehicle.lights.lights.keys() - seen_light_ids:
                        vehicle.lights.lights[light_id].enabled = False
                else:
                    self.update_enum(vehicle.lights.light_state, None, captured_at)
                    vehicle.lights.enabled = False

                log_extra_keys(LOG_API, "exteriorStatus", exterior_status, {"secure", "doorStatus", "doorLockStatus", "windowStatus", "lightStatus"})
            else:
                self.update_enum(vehicle.doors.lock_state, None, captured_at)
                self.update_enum(vehicle.doors.open_state, None, captured_at)
                vehicle.doors.enabled = False
                self.update_enum(vehicle.windows.open_state, None, captured_at)
                vehicle.windows.enabled = False
                self.update_enum(vehicle.lights.light_state, None, captured_at)
                vehicle.lights.enabled = False

            if isinstance(vehicle, VolkswagenNAElectricVehicle):
                climate_url = self.base_url + f"/ev/v1/vehicle/{vehicle.uuid}/climate/summary"
                climate_data: Dict[str, Any] | None = self._fetch_data(climate_url, self.session, token=token)
                if climate_data and "data" in climate_data:
                    climate_data = climate_data["data"]
                if climate_data and "carCapturedTimestamp" in climate_data:
                    captured_at: datetime = datetime.fromtimestamp((climate_data["carCapturedTimestamp"] / 1000), tz=timezone.utc)
                else:
                    raise APIError("Missing carCapturedTimestamp on climatization summary")
                if not isinstance(vehicle.climatization, VolkswagenClimatization):
                    vehicle.climatization = VolkswagenClimatization(origin=vehicle.climatization)

                if (
                    vehicle.climatization is not None
                    and vehicle.climatization.commands is not None
                    and not vehicle.climatization.commands.contains_command("start-stop")
                ):
                    start_stop_command = ClimatizationStartStopCommand(parent=vehicle.climatization.commands)
                    start_stop_command._add_on_set_hook(self.__on_air_conditioning_start_stop)  # pylint: disable=protected-access
                    start_stop_command.enabled = True
                    vehicle.climatization.commands.add_command(start_stop_command)
                if "climateStatusReport" in climate_data and climate_data["climateStatusReport"] is not None:
                    climatization_status = climate_data["climateStatusReport"]
                    if "carCapturedTimestamp" not in climatization_status or climatization_status["carCapturedTimestamp"] is None:
                        raise APIError("Could not fetch vehicle status, carCapturedTimestamp missing")
                    captured_at: datetime = datetime.fromtimestamp((climatization_status["carCapturedTimestamp"] / 1000), tz=timezone.utc)
                    if "climateStatusInd" in climatization_status and climatization_status["climateStatusInd"] is not None:
                        if climatization_status["climateStatusInd"] in [item.value for item in VolkswagenClimatization.ClimatizationState]:
                            climatization_state: VolkswagenClimatization.ClimatizationState = VolkswagenClimatization.ClimatizationState(
                                climatization_status["climateStatusInd"]
                            )
                        else:
                            LOG_API.info(
                                "Unknown climatization state %s not in %s",
                                climatization_status["climateStatusInd"],
                                str(VolkswagenClimatization.ClimatizationState),
                            )
                            climatization_state = VolkswagenClimatization.ClimatizationState.UNKNOWN
                        self.update_enum(vehicle.climatization.state, climatization_state, captured_at)
                    else:
                        self.update_enum(vehicle.climatization.state, None, captured_at)
                    if "remainingclimatizationTimeMin" in climatization_status and climatization_status["remainingclimatizationTimeMin"] is not None:
                        remaining_duration: timedelta = timedelta(minutes=climatization_status["remainingclimatizationTimeMin"])
                        estimated_date_reached: datetime = captured_at + remaining_duration
                        estimated_date_reached = estimated_date_reached.replace(second=0, microsecond=0)
                        self.update_datetime(vehicle.climatization.estimated_date_reached, estimated_date_reached, captured_at)
                    else:
                        self.update_datetime(vehicle.climatization.estimated_date_reached, None, captured_at)
                    log_extra_keys(
                        LOG_API,
                        "climateStatusReport",
                        climatization_status,
                        {
                            "carCapturedTimestamp",
                            "backendCapturedTimestamp",
                            "status",
                            "trigger",
                            "remainingClimatizationTimeMin",
                            "climateStatusInd",
                            "remainingclimatizationTimeMin",
                        },
                    )
                else:
                    self.update_enum(vehicle.climatization.state, None, captured_at)
                    self.update_datetime(vehicle.climatization.estimated_date_reached, None, captured_at)
                if "climateSettings" in climate_data and climate_data["climateSettings"] is not None:
                    climatization_settings = climate_data["climateSettings"]
                    if "carCapturedTimestamp" not in climatization_settings or climatization_settings["carCapturedTimestamp"] is None:
                        raise APIError("Could not fetch vehicle status, carCapturedTimestamp missing")
                    captured_at: datetime = datetime.fromtimestamp((climatization_settings["carCapturedTimestamp"] / 1000), tz=timezone.utc)
                    preferred_unit: Temperature = Temperature.C
                    precision: float = 0.5
                    if "targetTemperature" in climatization_settings and climatization_settings["targetTemperature"] is not None:
                        if "unit" in climatization_settings["targetTemperature"] and climatization_settings["targetTemperature"]["unit"] is not None:
                            if climatization_settings["targetTemperature"]["unit"] == "fahrenheit":
                                preferred_unit = Temperature.F
                                if isinstance(vehicle.climatization.settings, VolkswagenClimatization.Settings):
                                    vehicle.climatization.settings.unit_in_car = Temperature.F
                            elif climatization_settings["targetTemperature"]["unit"] == "celsius":
                                preferred_unit = Temperature.C
                                if isinstance(vehicle.climatization.settings, VolkswagenClimatization.Settings):
                                    vehicle.climatization.settings.unit_in_car = Temperature.C
                            else:
                                LOG_API.info("Unknown unitInCar %s", climatization_settings["targetTemperature"]["unit"])
                        target_temperature: float = climatization_settings["targetTemperature"]["temperature"]
                        if preferred_unit == Temperature.C:
                            min_temperature: Optional[float] = 16
                            max_temperature: Optional[float] = 29.5
                        elif preferred_unit == Temperature.F:
                            min_temperature: Optional[float] = 61
                            max_temperature: Optional[float] = 85
                        else:
                            min_temperature: Optional[float] = None
                            max_temperature: Optional[float] = None
                        LOG.debug("Updating target temperature to %s %s", target_temperature, preferred_unit.value)
                        self.update_float(
                            vehicle.climatization.settings.target_temperature,
                            target_temperature,
                            captured_at,
                            unit=preferred_unit,
                        )
                        vehicle.climatization.settings.target_temperature.minimum = min_temperature
                        vehicle.climatization.settings.target_temperature.maximum = max_temperature
                        vehicle.climatization.settings.target_temperature.precision = precision
                        # pylint: disable-next=protected-access
                        vehicle.climatization.settings.target_temperature._add_on_set_hook(self.__on_air_conditioning_settings_change)
                        vehicle.climatization.settings.target_temperature._is_changeable = True  # pylint: disable=protected-access
                    if (
                        "climatizationWithoutExternalPower" in climatization_settings
                        and climatization_settings["climatizationWithoutExternalPower"] is not None
                    ):
                        self.update_boolean(
                            vehicle.climatization.settings.climatization_without_external_power,
                            climatization_settings["climatizationWithoutExternalPower"],
                            captured_at,
                        )
                        # pylint: disable-next=protected-access
                        vehicle.climatization.settings.climatization_without_external_power._add_on_set_hook(self.__on_air_conditioning_settings_change)
                        vehicle.climatization.settings.climatization_without_external_power._is_changeable = True  # pylint: disable=protected-access
                    else:
                        self.update_boolean(
                            vehicle.climatization.settings.climatization_without_external_power,
                            None,
                            captured_at,
                        )

                    climatization_element_settings: Dict[str, Any] = {}
                    vclimatesettings = vehicle.climatization.settings
                    if not isinstance(vclimatesettings, VolkswagenClimatization.Settings):
                        return
                    if "climatizationElementSettings" in climatization_settings and climatization_settings["climatizationElementSettings"] is not None:
                        climatization_element_settings = climatization_settings["climatizationElementSettings"]

                    if (
                        climatization_element_settings
                        and "climatizationAtUnlock" in climatization_element_settings
                        and climatization_element_settings["climatizationAtUnlock"] is not None
                    ):
                        self.update_boolean(
                            vehicle.climatization.settings.climatization_at_unlock, climatization_element_settings["climatizationAtUnlock"], captured_at
                        )
                        # pylint: disable-next=protected-access
                        vehicle.climatization.settings.climatization_at_unlock._add_on_set_hook(self.__on_air_conditioning_settings_change)
                        vehicle.climatization.settings.climatization_at_unlock._is_changeable = True  # pylint: disable=protected-access
                    else:
                        self.update_boolean(vehicle.climatization.settings.climatization_at_unlock, None, captured_at)
                    if "windowHeatingEnabled" in climatization_element_settings and climatization_element_settings["windowHeatingEnabled"] is not None:
                        self.update_boolean(vehicle.climatization.settings.window_heating, climatization_element_settings["windowHeatingEnabled"], captured_at)
                        # pylint: disable-next=protected-access
                        vehicle.climatization.settings.window_heating._add_on_set_hook(self.__on_air_conditioning_settings_change)
                        vehicle.climatization.settings.window_heating._is_changeable = True  # pylint: disable=protected-access
                    else:
                        self.update_boolean(vehicle.climatization.settings.window_heating, None, captured_at)
                    if "zoneFrontLeftEnabled" in climatization_element_settings and climatization_element_settings["zoneFrontLeftEnabled"] is not None:
                        self.update_boolean(vclimatesettings.front_zone_left_enabled, climatization_element_settings["zoneFrontLeftEnabled"], captured_at)
                        # pylint: disable-next=protected-access
                        vclimatesettings.front_zone_left_enabled._add_on_set_hook(self.__on_air_conditioning_settings_change)
                        vclimatesettings.front_zone_left_enabled._is_changeable = True  # pylint: disable=protected-access
                    else:
                        self.update_boolean(vclimatesettings.front_zone_left_enabled, None, captured_at)
                    if "zoneFrontRightEnabled" in climatization_element_settings and climatization_element_settings["zoneFrontRightEnabled"] is not None:
                        self.update_boolean(vclimatesettings.front_zone_right_enabled, climatization_element_settings["zoneFrontRightEnabled"], captured_at)
                        # pylint: disable-next=protected-access
                        vclimatesettings.front_zone_right_enabled._add_on_set_hook(self.__on_air_conditioning_settings_change)
                        vclimatesettings.front_zone_right_enabled._is_changeable = True  # pylint: disable=protected-access
                    else:
                        self.update_boolean(vclimatesettings.front_zone_right_enabled, None, captured_at)
                    if "zoneRearLeftEnabled" in climatization_element_settings and climatization_element_settings["zoneRearLeftEnabled"] is not None:
                        self.update_boolean(vclimatesettings.rear_zone_left_enabled, climatization_element_settings["zoneRearLeftEnabled"], captured_at)
                        # pylint: disable-next=protected-access
                        vclimatesettings.rear_zone_left_enabled._add_on_set_hook(self.__on_air_conditioning_settings_change)
                        vclimatesettings.rear_zone_left_enabled._is_changeable = True  # pylint: disable=protected-access
                    else:
                        # pylint: disable-next=protected-access
                        self.update_boolean(vclimatesettings.rear_zone_left_enabled, None, captured_at)
                    if "zoneRearRightEnabled" in climatization_element_settings and climatization_element_settings["zoneRearRightEnabled"] is not None:
                        self.update_boolean(vclimatesettings.rear_zone_right_enabled, climatization_element_settings["zoneRearRightEnabled"], captured_at)
                        # pylint: disable-next=protected-access
                        vclimatesettings.rear_zone_right_enabled._add_on_set_hook(self.__on_air_conditioning_settings_change)
                        vclimatesettings.rear_zone_right_enabled._is_changeable = True  # pylint: disable=protected-access
                    else:
                        self.update_boolean(vclimatesettings.rear_zone_right_enabled, None, captured_at)
                    if (
                        vclimatesettings.front_zone_left_enabled.enabled
                        or vclimatesettings.front_zone_right_enabled.enabled
                        or vclimatesettings.rear_zone_left_enabled.enabled
                        or vclimatesettings.rear_zone_right_enabled.enabled
                    ):
                        if (
                            vclimatesettings.front_zone_left_enabled.value
                            or vclimatesettings.front_zone_right_enabled.value
                            or vclimatesettings.rear_zone_left_enabled.value
                            or vclimatesettings.rear_zone_right_enabled.value
                        ):
                            self.update_boolean(vehicle.climatization.settings.seat_heating, True, captured_at)
                        else:
                            self.update_boolean(vehicle.climatization.settings.seat_heating, False, captured_at)
                    else:
                        self.update_boolean(vehicle.climatization.settings.seat_heating, None, captured_at)
                    if "heaterSource" in climatization_element_settings and climatization_element_settings["heaterSource"] is not None:
                        if climatization_element_settings["heaterSource"] in [item.value for item in Climatization.Settings.HeaterSource]:
                            self.update_enum(
                                vehicle.climatization.settings.heater_source,
                                Climatization.Settings.HeaterSource(climatization_element_settings["heaterSource"]),
                                captured_at,
                            )
                        else:
                            LOG_API.info("Unknown heater source %s", climatization_element_settings["heaterSource"])
                            self.update_enum(vehicle.climatization.settings.heater_source, Climatization.Settings.HeaterSource.UNKNOWN, captured_at)
                    else:
                        self.update_enum(vehicle.climatization.settings.heater_source, None, captured_at)
                    log_extra_keys(
                        LOG_API,
                        "climatizationElementSettings",
                        climatization_element_settings,
                        {
                            "carCapturedTimestamp",
                            "climatizationWithoutExternalPower",
                            "climatizationAtUnlock",
                            "mirrorHeatingEnabled",
                            "windowHeatingEnabled",
                            "zoneFrontLeftEnabled",
                            "zoneFrontRightEnabled",
                            "zoneRearLeftEnabled",
                            "zoneRearRightEnabled",
                            "heaterSource",
                        },
                    )
                else:
                    vehicle.climatization.settings.target_temperature._set_value(None)  # pylint: disable=protected-access
                    vehicle.climatization.settings.climatization_without_external_power._set_value(None)  # pylint: disable=protected-access
                    vehicle.climatization.settings.climatization_at_unlock._set_value(None)  # pylint: disable=protected-access
                    vehicle.climatization.settings.window_heating._set_value(None)  # pylint: disable=protected-access
                    vehicle.climatization.settings.seat_heating._set_value(None)  # pylint: disable=protected-access
                    vehicle.climatization.settings.heater_source._set_value(None)  # pylint: disable=protected-access
                    if isinstance(vehicle.climatization.settings, VolkswagenClimatization.Settings):
                        vehicle.climatization.settings.front_zone_left_enabled._set_value(None)  # pylint: disable=protected-access
                        vehicle.climatization.settings.front_zone_right_enabled._set_value(None)  # pylint: disable=protected-access
                        vehicle.climatization.settings.rear_zone_left_enabled._set_value(None)  # pylint: disable=protected-access
                        vehicle.climatization.settings.rear_zone_right_enabled._set_value(None)  # pylint: disable=protected-access
                if "windowHeatingStatus" in climate_data and climate_data["windowHeatingStatus"] is not None:
                    if "value" in climate_data["windowHeatingStatus"] and climate_data["windowHeatingStatus"]["value"] is not None:
                        window_heating_status = climate_data["windowHeatingStatus"]["value"]
                        if "carCapturedTimestamp" not in window_heating_status or window_heating_status["carCapturedTimestamp"] is None:
                            raise APIError("Could not fetch vehicle status, carCapturedTimestamp missing")
                        captured_at: datetime = robust_time_parse(window_heating_status["carCapturedTimestamp"])
                        if "windowHeatingStatus" in window_heating_status and window_heating_status["windowHeatingStatus"] is not None:
                            heating_on: bool = False
                            all_heating_invalid: bool = True
                            for window_heating in window_heating_status["windowHeatingStatus"]:
                                if "windowLocation" in window_heating and window_heating["windowLocation"] is not None:
                                    window_id = window_heating["windowLocation"]
                                    if window_id in vehicle.window_heatings.windows:
                                        windowh: WindowHeatings.WindowHeating = vehicle.window_heatings.windows[window_id]
                                    else:
                                        windowh = WindowHeatings.WindowHeating(
                                            window_id=window_id,
                                            window_heatings=vehicle.window_heatings,
                                            initialization=vehicle.window_heatings.get_initialization(window_id),
                                        )
                                        vehicle.window_heatings.windows[window_id] = windowh
                                    if "windowHeatingState" in window_heating and window_heating["windowHeatingState"] is not None:
                                        if window_heating["windowHeatingState"] in [item.value for item in WindowHeatings.HeatingState]:
                                            window_heating_state: WindowHeatings.HeatingState = WindowHeatings.HeatingState(
                                                window_heating["windowHeatingState"]
                                            )
                                            if window_heating_state == WindowHeatings.HeatingState.ON:
                                                heating_on = True
                                            if window_heating_state in [WindowHeatings.HeatingState.ON, WindowHeatings.HeatingState.OFF]:
                                                all_heating_invalid = False
                                            self.update_enum(
                                                windowh.heating_state,
                                                window_heating_state,
                                                captured_at,
                                            )
                                        else:
                                            LOG_API.info(
                                                "Unknown window heating state %s not in %s",
                                                window_heating["windowHeatingState"],
                                                str(WindowHeatings.HeatingState),
                                            )
                                            # pylint: disable-next=protected-access
                                            self.update_enum(
                                                windowh.heating_state,
                                                WindowHeatings.HeatingState.UNKNOWN,
                                                captured_at,
                                            )
                                    else:
                                        self.update_enum(
                                            windowh.heating_state,
                                            None,
                                            captured_at,
                                        )
                                log_extra_keys(LOG_API, "windowHeatingStatus", window_heating, {"windowLocation", "windowHeatingState"})
                            if all_heating_invalid:
                                # pylint: disable-next=protected-access
                                self.update_enum(
                                    vehicle.window_heatings.heating_state,
                                    WindowHeatings.HeatingState.INVALID,
                                    captured_at,
                                )
                            else:
                                if heating_on:
                                    # pylint: disable-next=protected-access
                                    self.update_enum(
                                        vehicle.window_heatings.heating_state,
                                        WindowHeatings.HeatingState.ON,
                                        captured_at,
                                    )
                                else:
                                    # pylint: disable-next=protected-access
                                    self.update_enum(
                                        vehicle.window_heatings.heating_state,
                                        WindowHeatings.HeatingState.OFF,
                                        captured_at,
                                    )
                        if (
                            vehicle.window_heatings is not None
                            and vehicle.window_heatings.commands is not None
                            and not vehicle.window_heatings.commands.contains_command("start-stop")
                        ):
                            start_stop_command = WindowHeatingStartStopCommand(parent=vehicle.window_heatings.commands)
                            start_stop_command._add_on_set_hook(self.__on_window_heating_start_stop)  # pylint: disable=protected-access
                            start_stop_command.enabled = True
                            vehicle.window_heatings.commands.add_command(start_stop_command)
                        log_extra_keys(LOG_API, "windowHeatingStatus", window_heating_status, {"carCapturedTimestamp", "windowHeatingStatus"})

                log_extra_keys(
                    LOG_API,
                    "climatization",
                    climate_data,
                    {
                        "climatizationStatus",
                        "carCapturedTimestamp",
                        "backendCapturedTimestamp",
                        "windowHeatingReport",
                        "climateTimerSetting",
                        "climateSettings",
                        "climateStatusReport",
                        "temperature",
                        "windowHeatingStatus",
                    },
                )

            if isinstance(vehicle, VolkswagenNAElectricVehicle):
                if vehicle.charging is not None and vehicle.charging.commands is not None and not vehicle.charging.commands.contains_command("start-stop"):
                    start_stop_command = ChargingStartStopCommand(parent=vehicle.charging.commands)
                    start_stop_command._add_on_set_hook(self.__on_charging_start_stop)  # pylint: disable=protected-access
                    start_stop_command.enabled = True
                    vehicle.charging.commands.add_command(start_stop_command)

                charge_url = self.base_url + f"/ev/v1/vehicle/{vehicle.uuid}/charge/summary"
                charge_data: Dict[str, Any] | None = self._fetch_data(charge_url, self.session, token=token)
                if charge_data and "data" in charge_data:
                    charge_data = charge_data["data"]
                if charge_data and "carCapturedTimestamp" in charge_data:
                    captured_at: datetime = datetime.fromtimestamp((charge_data["carCapturedTimestamp"] / 1000), tz=timezone.utc)
                else:
                    raise APIError("Missing carCapturedTimestamp on EV Charge summary")

                if "batteryStatus" in charge_data and charge_data["batteryStatus"] is not None:
                    battery_status = charge_data["batteryStatus"]
                    if "carCapturedTimestamp" not in battery_status or battery_status["carCapturedTimestamp"] is None:
                        raise APIError("Could not fetch vehicle status, carCapturedTimestamp missing")
                    captured_at: datetime = datetime.fromtimestamp((battery_status["carCapturedTimestamp"] / 1000), tz=timezone.utc)
                    if "currentSOCPct" in battery_status and battery_status["currentSOCPct"] is not None:
                        drive = vehicle.drives.drives["primary"]
                        self.update_float(drive.level, float(battery_status["currentSOCPct"]), captured_at)
                        drive.level.precision = 1

                if "chargingStatus" in charge_data and charge_data["chargingStatus"] is not None:
                    charging_status = charge_data["chargingStatus"]
                    if "carCapturedTimestamp" not in charging_status or charging_status["carCapturedTimestamp"] is None:
                        raise APIError("Could not fetch vehicle status, carCapturedTimestamp missing")
                    captured_at: datetime = datetime.fromtimestamp((charging_status["carCapturedTimestamp"] / 1000), tz=timezone.utc)

                    if charging_status["currentChargeState"] in [item.value for item in VolkswagenNACharging.VolkswagenChargingState]:
                        volkswagen_charging_state = VolkswagenNACharging.VolkswagenChargingState(charging_status["currentChargeState"])
                        charging_state: Charging.ChargingState = mapping_volskwagen_charging_state[volkswagen_charging_state]
                    else:
                        LOG_API.info(
                            "Unkown charging state %s not in %s", charging_status["currentChargeState"], str(VolkswagenNACharging.VolkswagenChargingState)
                        )
                        charging_state = Charging.ChargingState.UNKNOWN

                    self.update_enum(
                        vehicle.charging.state,
                        charging_state,
                        captured_at,
                    )
                    if "chargeType" in charging_status and charging_status["chargeType"] is not None:
                        if charging_status["chargeType"] in [item.value for item in Charging.ChargingType]:
                            self.update_enum(
                                vehicle.charging.type,
                                Charging.ChargingType(charging_status["chargeType"]),
                                captured_at,
                            )
                        else:
                            LOG_API.info("Unknown charge type %s", charging_status["chargeType"])
                            self.update_enum(
                                vehicle.charging.type,
                                Charging.ChargingType.UNKNOWN,
                                captured_at,
                            )
                    else:
                        self.update_enum(
                            vehicle.charging.type,
                            Charging.ChargingType.UNKNOWN,
                            captured_at,
                        )
                    if "chargePower" in charging_status and charging_status["chargePower"] is not None:
                        self.update_float(vehicle.charging.power, float(charging_status["chargePower"]), captured_at, unit=Power.KW)
                    else:
                        self.update_float(vehicle.charging.power, None, captured_at, unit=Power.KW)
                    if "chargeRate" in charging_status and charging_status["chargeRate"] is not None:
                        self.update_float(vehicle.charging.rate, float(charging_status["chargeRate"]), captured_at, unit=Speed.KMH)
                    else:
                        self.update_float(vehicle.charging.rate, None, captured_at, unit=Speed.KMH)
                    if "remainingChargingTimeToComplete" in charging_status and charging_status["remainingChargingTimeToComplete"] is not None:
                        remaining_duration: timedelta = timedelta(minutes=charging_status["remainingChargingTimeToComplete"])
                        estimated_date_reached: datetime = captured_at + remaining_duration
                        estimated_date_reached = estimated_date_reached.replace(second=0, microsecond=0)
                        self.update_datetime(vehicle.charging.estimated_date_reached, estimated_date_reached, captured_at)
                    else:
                        self.update_datetime(vehicle.charging.estimated_date_reached, None, captured_at)
                    log_extra_keys(
                        LOG_API,
                        "chargingStatus",
                        charging_status,
                        {
                            "chargeMode",
                            "chargeType",
                            "currentChargeState",
                            "carCapturedTimestamp",
                            "chargePower",
                            "profileChargeReason",
                            "remainingChargingTimeToComplete",
                            "chargeRate",
                            "status",
                            "chargeTargetTime",
                            "chargingScenario",
                        },
                    )
                else:
                    self.update_enum(vehicle.charging.state, None, captured_at)  # pylint: disable=protected-access

                if "chargeSettings" in charge_data and charge_data["chargeSettings"] is not None:
                    charging_settings = charge_data["chargeSettings"]
                    if "carCapturedTimestamp" not in charging_settings or charging_settings["carCapturedTimestamp"] is None:
                        raise APIError("Could not fetch vehicle status, carCapturedTimestamp missing")
                    captured_at: datetime = datetime.fromtimestamp((charging_settings["carCapturedTimestamp"] / 1000), tz=timezone.utc)
                    if "maxChargingCurrent" in charging_settings and charging_settings["maxChargingCurrent"] is not None:
                        if isinstance(vehicle.charging.settings, VolkswagenNACharging.Settings):
                            vehicle.charging.settings.max_current_in_ampere = False
                        else:
                            raise ValueError("Charging settings not of type VolkswagenNACharging.Settings")

                        if charging_settings["maxChargingCurrent"] == "max":
                            charging_settings["maxChargingCurrent"] = 32.0
                        else:
                            charging_settings["maxChargingCurrent"] = 10.0
                        vehicle.charging.settings.maximum_current.minimum = 10.0
                        vehicle.charging.settings.maximum_current.maximum = 32.0
                        vehicle.charging.settings.maximum_current.precision = 22.0
                        # pylint: disable-next=protected-access
                        vehicle.charging.settings.maximum_current._add_on_set_hook(self.__on_charging_settings_change)
                        vehicle.charging.settings.maximum_current._is_changeable = True  # pylint: disable=protected-access
                        self.update_float(
                            vehicle.charging.settings.maximum_current,
                            charging_settings["maxChargingCurrent"],
                            captured_at,
                        )
                    else:
                        self.update_float(vehicle.charging.settings.maximum_current, None, captured_at)
                    if "autoUnlockPlugWhenCharged" in charging_settings and charging_settings["autoUnlockPlugWhenCharged"] is not None:
                        # pylint: disable-next=protected-access
                        vehicle.charging.settings.auto_unlock._add_on_set_hook(self.__on_charging_settings_change)
                        vehicle.charging.settings.auto_unlock._is_changeable = True  # pylint: disable=protected-access
                        if charging_settings["autoUnlockPlugWhenCharged"] == "on" or charging_settings["autoUnlockPlugWhenCharged"] == "permanent":
                            self.update_boolean(vehicle.charging.settings.auto_unlock, True, captured_at)
                        elif charging_settings["autoUnlockPlugWhenCharged"] == "off":
                            self.update_boolean(vehicle.charging.settings.auto_unlock, False, captured_at)
                        else:
                            LOG_API.info("Unknown auto unlock plug when charged %s", charging_settings["autoUnlockPlugWhenCharged"])
                            self.update_boolean(vehicle.charging.settings.auto_unlock, None, captured_at)
                    else:
                        self.update_boolean(vehicle.charging.settings.auto_unlock, None, captured_at)
                    if "targetSOCPercentage" in charging_settings and charging_settings["targetSOCPercentage"] is not None:
                        vehicle.charging.settings.target_level.minimum = 50.0
                        vehicle.charging.settings.target_level.maximum = 100.0
                        vehicle.charging.settings.target_level.precision = 10.0
                        # pylint: disable-next=protected-access
                        vehicle.charging.settings.target_level._add_on_set_hook(self.__on_charging_settings_change)
                        vehicle.charging.settings.target_level._is_changeable = True  # pylint: disable=protected-access
                        self.update_float(vehicle.charging.settings.target_level, float(charging_settings["targetSOCPercentage"]), captured_at)
                    else:
                        self.update_float(vehicle.charging.settings.target_level, None, captured_at)
                    log_extra_keys(
                        LOG_API,
                        "chargingSettings",
                        charging_settings,
                        {"carCapturedTimestamp", "maxChargingCurrent", "autoUnlockPlugWhenCharged", "targetSOCPercentage", "status", "chargeModeSelection"},
                    )
                else:
                    vehicle.charging.settings.maximum_current._set_value(None)  # pylint: disable=protected-access
                    vehicle.charging.settings.auto_unlock._set_value(None)  # pylint: disable=protected-access
                    vehicle.charging.settings.target_level._set_value(None)  # pylint: disable=protected-access
                if "plugStatus" in charge_data and charge_data["plugStatus"] is not None:
                    plug_status = charge_data["plugStatus"]
                    if "carCapturedTimestamp" not in plug_status or plug_status["carCapturedTimestamp"] is None:
                        raise APIError("Could not fetch vehicle status, carCapturedTimestamp missing")
                    captured_at: datetime = datetime.fromtimestamp((plug_status["carCapturedTimestamp"] / 1000), tz=timezone.utc)
                    if "plugConnectionState" in plug_status and plug_status["plugConnectionState"] is not None:
                        if plug_status["plugConnectionState"] in [item.value for item in ChargingConnector.ChargingConnectorConnectionState]:
                            plug_state: ChargingConnector.ChargingConnectorConnectionState = ChargingConnector.ChargingConnectorConnectionState(
                                plug_status["plugConnectionState"]
                            )
                        else:
                            LOG_API.info(
                                "Unknown plug connection state %s not in %s",
                                plug_status["plugConnectionState"],
                                str(ChargingConnector.ChargingConnectorConnectionState),
                            )
                            plug_state = ChargingConnector.ChargingConnectorConnectionState.UNKNOWN

                        self.update_enum(vehicle.charging.connector.connection_state, plug_state, captured_at)
                    else:
                        self.update_enum(vehicle.charging.connector.connection_state, None, captured_at)
                    if "plugLockState" in plug_status and plug_status["plugLockState"] is not None:
                        if plug_status["plugLockState"] in [item.value for item in ChargingConnector.ChargingConnectorLockState]:
                            plug_lock_state: ChargingConnector.ChargingConnectorLockState = ChargingConnector.ChargingConnectorLockState(
                                plug_status["plugLockState"]
                            )
                        else:
                            LOG_API.info(
                                "Unknown plug lock state %s not in %s", plug_status["plugLockState"], str(ChargingConnector.ChargingConnectorLockState)
                            )
                            plug_lock_state = ChargingConnector.ChargingConnectorLockState.UNKNOWN
                        self.update_enum(vehicle.charging.connector.lock_state, plug_lock_state, captured_at)
                    else:
                        self.update_enum(vehicle.charging.connector.lock_state, None, captured_at)
                    if "infrastructureState" in plug_status and plug_status["infrastructureState"] is not None:
                        if plug_status["infrastructureState"] == "ready":
                            plug_status["infrastructureState"] = ChargingConnector.ExternalPower.AVAILABLE.value
                        if plug_status["infrastructureState"] in [item.value for item in ChargingConnector.ExternalPower]:
                            external_power: ChargingConnector.ExternalPower = ChargingConnector.ExternalPower(plug_status["infrastructureState"])
                        else:
                            LOG_API.info("Unknown external power %s not in %s", plug_status["infrastructureState"], str(ChargingConnector.ExternalPower))
                            external_power = ChargingConnector.ExternalPower.UNKNOWN
                        self.update_enum(vehicle.charging.connector.external_power, external_power, captured_at)
                    else:
                        self.update_enum(vehicle.charging.connector.external_power, None, captured_at)
                    log_extra_keys(
                        LOG_API,
                        "plugStatus",
                        plug_status,
                        {"carCapturedTimestamp", "backendCapturedTimestamp", "status", "plugConnectionState", "plugLockState", "infrastructureState"},
                    )

            if "vehicleHealthInspection" in data and data["vehicleHealthInspection"] is not None:
                if "maintenanceStatus" in data["vehicleHealthInspection"] and data["vehicleHealthInspection"]["maintenanceStatus"] is not None:
                    if (
                        "value" in data["vehicleHealthInspection"]["maintenanceStatus"]
                        and data["vehicleHealthInspection"]["maintenanceStatus"]["value"] is not None
                    ):
                        maintenance_status = data["vehicleHealthInspection"]["maintenanceStatus"]["value"]
                        if "carCapturedTimestamp" not in maintenance_status or maintenance_status["carCapturedTimestamp"] is None:
                            raise APIError("Could not fetch vehicle status, carCapturedTimestamp missing")
                        captured_at: datetime = robust_time_parse(maintenance_status["carCapturedTimestamp"])
                        if "inspectionDue_days" in maintenance_status and maintenance_status["inspectionDue_days"] is not None:
                            inspection_due: timedelta = timedelta(days=maintenance_status["inspectionDue_days"])
                            inspection_date: datetime = captured_at + inspection_due
                            inspection_date = inspection_date.replace(hour=0, minute=0, second=0, microsecond=0)
                            self.update_datetime(vehicle.maintenance.inspection_due_at, inspection_date, captured_at)
                        else:
                            self.update_datetime(vehicle.maintenance.inspection_due_at, None, captured_at)
                        if "inspectionDue_km" in maintenance_status and maintenance_status["inspectionDue_km"] is not None:
                            # pylint: disable-next=protected-access
                            self.update_float(vehicle.maintenance.inspection_due_after, maintenance_status["inspectionDue_km"], captured_at, Length.KM)
                            vehicle.maintenance.inspection_due_after.precision = 1
                        else:
                            vehicle.maintenance.inspection_due_after._set_value(None)  # pylint: disable=protected-access
                        if "oilServiceDue_days" in maintenance_status and maintenance_status["oilServiceDue_days"] is not None:
                            oil_service_due: timedelta = timedelta(days=maintenance_status["oilServiceDue_days"])
                            oil_service_date: datetime = captured_at + oil_service_due
                            oil_service_date = oil_service_date.replace(hour=0, minute=0, second=0, microsecond=0)
                            self.update_datetime(vehicle.maintenance.oil_service_due_at, oil_service_date, captured_at)
                        else:
                            self.update_datetime(vehicle.maintenance.oil_service_due_at, None, captured_at)
                        if "oilServiceDue_km" in maintenance_status and maintenance_status["oilServiceDue_km"] is not None:
                            # pylint: disable-next=protected-access
                            self.update_float(vehicle.maintenance.oil_service_due_after, maintenance_status["oilServiceDue_km"], captured_at, Length.KM)
                            vehicle.maintenance.oil_service_due_after.precision = 1
                        else:
                            self.update_float(vehicle.maintenance.oil_service_due_after, None, captured_at, Length.KM)
                        if (
                            "mileage_km" in maintenance_status
                            and maintenance_status["mileage_km"] is not None
                            and not vehicle.odometer.enabled
                            and vehicle.odometer is None
                        ):
                            # pylint: disable-next=protected-access
                            vehicle.odometer._set_value(value=maintenance_status["mileage_km"], measured=captured_at, unit=Length.KM)
                            vehicle.odometer.precision = 1

                        log_extra_keys(
                            LOG_API,
                            "maintenanceStatus",
                            maintenance_status,
                            {"carCapturedTimestamp", "inspectionDue_days", "inspectionDue_km", "oilServiceDue_days", "oilServiceDue_km", "mileage_km"},
                        )
                log_extra_keys(LOG_API, "vehicleHealthInspection", data["vehicleHealthInspection"], {"maintenanceStatus"})
            if "readiness" in data and data["readiness"] is not None:
                if "readinessStatus" in data["readiness"] and data["readiness"]["readinessStatus"] is not None:
                    readiness_status = data["readiness"]["readinessStatus"]
                    if "value" in readiness_status and readiness_status["value"] is not None:
                        readiness_status = readiness_status["value"]
                        if "connectionState" in readiness_status and readiness_status["connectionState"] is not None:
                            if "isOnline" in readiness_status["connectionState"] and readiness_status["connectionState"]["isOnline"] is not None:
                                if readiness_status["connectionState"]["isOnline"] is True:
                                    vehicle.connection_state._set_value(GenericVehicle.ConnectionState.REACHABLE)  # pylint: disable=protected-access
                                else:
                                    vehicle.connection_state._set_value(GenericVehicle.ConnectionState.OFFLINE)  # pylint: disable=protected-access
                            else:
                                vehicle.connection_state._set_value(None)  # pylint: disable=protected-access
                            if "isActive" in readiness_status["connectionState"] and readiness_status["connectionState"]["isActive"] is not None:
                                vehicle.is_active._set_value(readiness_status["connectionState"]["isActive"])  # pylint: disable=protected-access
                            else:
                                vehicle.is_active._set_value(None)  # pylint: disable=protected-access
                            log_extra_keys(LOG_API, "connectionState", readiness_status["connectionState"], {"isOnline", "isActive"})
                        log_extra_keys(LOG_API, "readinessStatus", readiness_status, {"connectionState"})
            log_extra_keys(
                LOG_API,
                "selectivestatus",
                data,
                {
                    "timestamp",
                    "location",
                    "lockStatus",
                    "doorStatus",
                    "windowStatus",
                    "lightStatus",
                    "clampState",
                    "clampStateTimestamp",
                    "currentMileage",
                    "powerStatus",
                    "exteriorStatus",
                    "instrumentCluserTime",
                    "platform",
                    "nextMaintenanceMilestone",
                    "lastParkedLocation",
                },
            )

    def _record_elapsed(self, elapsed: timedelta) -> None:
        """
        Records the elapsed time.

        Args:
            elapsed (timedelta): The elapsed time to record.
        """
        self._elapsed.append(elapsed)

    def _fetch_data(self, url, session, force=False, allow_empty=False, allow_http_error=False, allowed_errors=None, token=None) -> Optional[Dict[str, Any]]:  # noqa: C901
        data: Optional[Dict[str, Any]] = None
        cache_date: Optional[datetime] = None
        if not force and (self.active_config["max_age"] is not None and session.cache is not None and url in session.cache):
            data, cache_date_string = session.cache[url]
            cache_date = datetime.fromisoformat(cache_date_string)
        if (
            data is None
            or self.active_config["max_age"] is None
            or (cache_date is not None and cache_date < (datetime.utcnow() - timedelta(seconds=self.active_config["max_age"])))
        ):
            try:
                status_response: requests.Response = session.get(url, allow_redirects=False, token=token)
                self._record_elapsed(status_response.elapsed)
                if status_response.status_code in (requests.codes["ok"], requests.codes["multiple_status"]):
                    data = status_response.json()
                    if session.cache is not None:
                        session.cache[url] = (data, str(datetime.utcnow()))
                elif status_response.status_code == requests.codes["no_content"] and allow_empty:
                    data = None
                elif status_response.status_code == requests.codes["too_many_requests"]:
                    raise TooManyRequestsError(
                        f"Could not fetch data due to too many requests from your account. Status Code was: {status_response.status_code}"
                    )
                elif status_response.status_code == requests.codes["unauthorized"]:
                    LOG.info("Server asks for new authorization")
                    session.login()
                    status_response = session.get(url, allow_redirects=False, token=token)

                    if status_response.status_code in (requests.codes["ok"], requests.codes["multiple_status"]):
                        data = status_response.json()
                        if session.cache is not None:
                            session.cache[url] = (data, str(datetime.utcnow()))
                    elif not allow_http_error or (allowed_errors is not None and status_response.status_code not in allowed_errors):
                        raise RetrievalError(f"Could not fetch data even after re-authorization. Status Code was: {status_response.status_code}")
                elif not allow_http_error or (allowed_errors is not None and status_response.status_code not in allowed_errors):
                    raise RetrievalError(f"Could not fetch data for {url}. Status Code was: {status_response.status_code}")
            except requests.exceptions.ConnectionError as connection_error:
                raise RetrievalError(f"Connection error: {connection_error}") from connection_error
            except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
                raise RetrievalError(f"Error: {chunked_encoding_error}") from chunked_encoding_error
            except requests.exceptions.ReadTimeout as timeout_error:
                raise RetrievalError(f"Timeout during read: {timeout_error}") from timeout_error
            except requests.exceptions.RetryError as retry_error:
                raise RetrievalError(f"Retrying failed: {retry_error}") from retry_error
            except requests.exceptions.JSONDecodeError as json_error:
                if allow_empty:
                    data = None
                else:
                    raise RetrievalError(f"JSON decode error: {json_error}") from json_error
        return data

    def get_version(self) -> str:
        return __version__

    def get_type(self) -> str:
        return "carconnectivity-connector-volkswagen-na"

    def __on_air_conditioning_settings_change(self, attribute: GenericAttribute, value: Any) -> Any:
        """
        Callback for the climatization setting change.
        """
        if (
            attribute.parent is None
            or not isinstance(attribute.parent, VolkswagenClimatization.Settings)
            or attribute.parent.parent is None
            or attribute.parent.parent.parent is None
            or not isinstance(attribute.parent.parent.parent, VolkswagenNAVehicle)
        ):
            raise SetterError("Object hierarchy is not as expected")
        settings: VolkswagenClimatization.Settings = attribute.parent
        vehicle: VolkswagenNAVehicle = attribute.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        vuuid: Optional[str] = vehicle.uuid.value
        if vin is None:
            raise SetterError("VIN in object hierarchy missing")
        if vuuid is None:
            raise CommandError("UUID in object hierarchy missing")
        setting_dict = {"climatizationElementSettings": {}, "targetTemperature": {}, "climatizationWithoutExternalPower": None}
        if settings.target_temperature.enabled and settings.target_temperature.value is not None:
            # Round target temperature to nearest 0.5
            # Check if the attribute changed is the target_temperature attribute
            precision: float = settings.target_temperature.precision if settings.target_temperature.precision is not None else 0.5
            if isinstance(attribute, TemperatureAttribute) and attribute.id == "target_temperature":
                value = round(value / precision) * precision
                setting_dict["targetTemperature"]["temperature"] = value
            else:
                setting_dict["targetTemperature"]["temperature"] = round(settings.target_temperature.value / precision) * precision
            setting_dict["targetTemperature"]["measurementState"] = "valid"
            if settings.unit_in_car == Temperature.C:
                setting_dict["targetTemperature"]["unit"] = "celsius"
            elif settings.unit_in_car == Temperature.F:
                setting_dict["targetTemperature"]["unit"] = "fahrenheit"
            elif settings.target_temperature.unit == Temperature.F:
                setting_dict["targetTemperature"]["unit"] = "fahrenheit"
            else:
                setting_dict["targetTemperature"]["unit"] = "celsius"
        if isinstance(attribute, BooleanAttribute) and attribute.id == "climatization_without_external_power":
            setting_dict["climatizationWithoutExternalPower"] = value
        elif settings.climatization_without_external_power.enabled and settings.climatization_without_external_power.value is not None:
            setting_dict["climatizationWithoutExternalPower"] = settings.climatization_without_external_power.value
        if isinstance(attribute, BooleanAttribute) and attribute.id == "climatization_at_unlock":
            setting_dict["climatizationElementSettings"]["climatizationAtUnlock"] = value
        elif settings.climatization_at_unlock.enabled and settings.climatization_at_unlock.value is not None:
            setting_dict["climatizationElementSettings"]["climatizationAtUnlock"] = settings.climatization_at_unlock.value
        if isinstance(attribute, BooleanAttribute) and attribute.id == "window_heating":
            setting_dict["climatizationElementSettings"]["windowHeatingEnabled"] = value
        elif settings.window_heating.enabled and settings.window_heating.value is not None:
            setting_dict["climatizationElementSettings"]["windowHeatingEnabled"] = settings.window_heating.value
        if isinstance(attribute, BooleanAttribute) and attribute.id == "front_zone_left_enabled":
            setting_dict["climatizationElementSettings"]["zoneFrontLeftEnabled"] = value
        elif settings.front_zone_left_enabled.enabled and settings.front_zone_left_enabled.value is not None:
            setting_dict["climatizationElementSettings"]["zoneFrontLeftEnabled"] = settings.front_zone_left_enabled.value
        if isinstance(attribute, BooleanAttribute) and attribute.id == "front_zone_right_enabled":
            setting_dict["climatizationElementSettings"]["zoneFrontRightEnabled"] = value
        elif settings.front_zone_right_enabled.enabled and settings.front_zone_right_enabled.value is not None:
            setting_dict["climatizationElementSettings"]["zoneFrontRightEnabled"] = settings.front_zone_right_enabled.value
        if isinstance(attribute, BooleanAttribute) and attribute.id == "rear_zone_left_enabled":
            setting_dict["climatizationElementSettings"]["zoneRearLeftEnabled"] = value
        elif settings.rear_zone_left_enabled.enabled and settings.rear_zone_left_enabled.value is not None:
            setting_dict["climatizationElementSettings"]["zoneRearLeftEnabled"] = settings.rear_zone_left_enabled.value
        if isinstance(attribute, BooleanAttribute) and attribute.id == "rear_zone_right_enabled":
            setting_dict["climatizationElementSettings"]["zoneRearRightEnabled"] = value
        elif settings.rear_zone_right_enabled.enabled and settings.rear_zone_right_enabled.value is not None:
            setting_dict["climatizationElementSettings"]["zoneRearRightEnabled"] = settings.rear_zone_right_enabled.value

        unit = setting_dict["targetTemperature"]["unit"]
        url: str = self.base_url + f"/ev/v1/vehicle/{vuuid}/pretripclimate/settings?tempUnit={unit}"
        LOG.debug("Setting climatization settings for vehicle %s to %s", vin, str(setting_dict))
        try:
            token = self.__do_spin(vehicle)
        except HTTPError as http_error:
            LOG.info(f"Could not fetch SPIN token, trying to execute climatization settings change without token. Error was: {http_error}")
            token = None

        try:
            settings_response: requests.Response = self.session.put(url, data=json.dumps(setting_dict), allow_redirects=True, token=token)
            if settings_response.status_code != requests.codes["ok"]:
                LOG.error("Could not set climatization settings (%s): %s", settings_response.status_code, settings_response.text)
                raise SetterError(f"Could not set value ({settings_response.status_code})")
        except requests.exceptions.ConnectionError as connection_error:
            raise SetterError(
                f"Connection error: {connection_error}. If this happens frequently, please check if other applications communicate with the Volkswagen server."
            ) from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise SetterError(f"Error: {chunked_encoding_error}") from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise SetterError(f"Timeout during read: {timeout_error}") from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise SetterError(f"Retrying failed: {retry_error}") from retry_error
        return value

    def __on_air_conditioning_start_stop(
        self, start_stop_command: ClimatizationStartStopCommand, command_arguments: Union[str, Dict[str, Any]]
    ) -> Union[str, Dict[str, Any]]:
        if (
            start_stop_command.parent is None
            or start_stop_command.parent.parent is None
            or start_stop_command.parent.parent.parent is None
            or not isinstance(start_stop_command.parent.parent.parent, VolkswagenNAVehicle)
        ):
            raise CommandError("Object hierarchy is not as expected")
        if not isinstance(command_arguments, dict):
            raise CommandError("Command arguments are not a dictionary")
        vehicle: VolkswagenNAVehicle = start_stop_command.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        vuuid: Optional[str] = vehicle.uuid.value
        if vin is None:
            raise CommandError("VIN in object hierarchy missing")
        if vuuid is None:
            raise CommandError("UUID in object hierarchy missing")
        if "command" not in command_arguments:
            raise CommandError("Command argument missing")
        command_str: Optional[str] = None
        if command_arguments["command"] == ClimatizationStartStopCommand.Command.START:
            command_str = "start"
        elif command_arguments["command"] == ClimatizationStartStopCommand.Command.STOP:
            command_str = "stop"
        else:
            raise CommandError(f"Unknown command {command_arguments['command']}")

        url: str = self.base_url + f"/ev/v1/vehicle/{vuuid}/pretripclimate/{command_str}"
        try:
            token = self.__do_spin(vehicle)
        except HTTPError as http_error:
            LOG.info(f"Could not fetch SPIN token, trying to execute air conditioning command without token. Error was: {http_error}")
            token = None
        try:
            command_response: requests.Response = self.session.post(url, allow_redirects=True, token=token)
            if command_response.status_code != requests.codes["ok"]:
                LOG.error("Could not start/stop air conditioning (%s: %s)", command_response.status_code, command_response.text)
                raise CommandError(f"Could not start/stop air conditioning ({command_response.status_code}: {command_response.text})")
            # Optimistic state update: immediately reflect the expected climatization state
            # so the UI doesn't show stale data until the next poll (up to 600s)
            optimistic_time = datetime.now(tz=timezone.utc)
            if command_arguments["command"] == ClimatizationStartStopCommand.Command.START:
                self.update_enum(vehicle.climatization.state, Climatization.ClimatizationState.HEATING, optimistic_time)
            elif command_arguments["command"] == ClimatizationStartStopCommand.Command.STOP:
                self.update_enum(vehicle.climatization.state, Climatization.ClimatizationState.OFF, optimistic_time)
            LOG.debug("Optimistic state update: climatization state set to %s", vehicle.climatization.state.value)
        except requests.exceptions.ConnectionError as connection_error:
            raise CommandError(
                f"Connection error: {connection_error}. If this happens frequently, please check if other applications communicate with the Volkswagen server."
            ) from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise CommandError(f"Error: {chunked_encoding_error}") from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise CommandError(f"Timeout during read: {timeout_error}") from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise CommandError(f"Retrying failed: {retry_error}") from retry_error
        return command_arguments

    def __on_honk_flash(self, honk_flash_command: HonkAndFlashCommand, command_arguments: Union[str, Dict[str, Any]]) -> Union[str, Dict[str, Any]]:
        # Honk and Flash doesn't work with 2020-2024 id.4's. I don't have another car to test with
        raise CommandError("HonkAndFlash not implemented")

    def __on_lock_unlock(self, lock_unlock_command: LockUnlockCommand, command_arguments: Union[str, Dict[str, Any]]) -> Union[str, Dict[str, Any]]:
        if (
            lock_unlock_command.parent is None
            or lock_unlock_command.parent.parent is None
            or lock_unlock_command.parent.parent.parent is None
            or not isinstance(lock_unlock_command.parent.parent.parent, VolkswagenNAVehicle)
        ):
            raise CommandError("Object hierarchy is not as expected")
        if not isinstance(command_arguments, dict):
            raise SetterError("Command arguments are not a dictionary")
        vehicle: VolkswagenNAVehicle = lock_unlock_command.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        vuuid: Optional[str] = vehicle.uuid.value
        if vin is None:
            raise CommandError("VIN in object hierarchy missing")
        if "command" not in command_arguments:
            raise CommandError("Command argument missing")
        command_dict = {}
        url = self.base_url + f"/lockunlock/v1/vehicle/{vuuid}"
        if command_arguments["command"] == LockUnlockCommand.Command.LOCK:
            command_dict["lock"] = True
        elif command_arguments["command"] == LockUnlockCommand.Command.UNLOCK:
            command_dict["lock"] = False
        else:
            raise CommandError(f"Unknown command {command_arguments['command']}")

        try:
            token = self.__do_spin(vehicle)
        except HTTPError as http_error:
            LOG.info(f"Could not fetch SPIN token, trying to execute locking command without token. Error was: {http_error}")
            token = None
        if token is None:
            raise CommandError("Could not fetch SPIN token, locking command cannot be executed")

        LOG.info("Performing lock unlock command for vehicle %s with command %s", vin, json.dumps(command_dict))
        try:
            command_response: requests.Response = self.session.put(url, data=json.dumps(command_dict), allow_redirects=True, token=token)
            if command_response.status_code != requests.codes["ok"]:
                LOG.error("Could not execute locking command (%s: %s)", command_response.status_code, command_response.text)
                raise CommandError(f"Could not execute locking command ({command_response.status_code}: {command_response.text})")
            LOG.info("Locking command executed successfully, response: %s", command_response.text)
            # Optimistic state update: immediately reflect the expected state locally
            # so the UI doesn't show stale data until the next poll (up to 600s)
            optimistic_time = datetime.now(tz=timezone.utc)
            if command_arguments["command"] == LockUnlockCommand.Command.LOCK:
                optimistic_lock_state = Doors.LockState.LOCKED
            else:
                optimistic_lock_state = Doors.LockState.UNLOCKED
            LOG.debug("Optimistic state update: setting lock_state to %s", optimistic_lock_state)
            if vehicle.doors is not None:
                self.update_enum(vehicle.doors.lock_state, optimistic_lock_state, optimistic_time)
                for door in vehicle.doors.doors.values():
                    if door.lock_state.enabled:
                        self.update_enum(door.lock_state, optimistic_lock_state, optimistic_time)
        except requests.exceptions.ConnectionError as connection_error:
            raise CommandError(
                f"Connection error: {connection_error}. If this happens frequently, please check if other applications communicate with the Volkswagen server."
            ) from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise CommandError(f"Error: {chunked_encoding_error}") from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise CommandError(f"Timeout during read: {timeout_error}") from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise CommandError(f"Retrying failed: {retry_error}") from retry_error
        return command_arguments

    def __do_set_spin(self, vehicle: VolkswagenNAVehicle, spin: str | None = None) -> bool:  # pylint: disable=unused-private-member
        if not isinstance(vehicle, VolkswagenNAVehicle):
            raise CommandError("Object is not a VolkswagenNAVehicle")
        if spin is None:
            if self.active_config["spin"] is None or self.active_config["spin"] == "":
                LOG.warning("S-PIN is missing, please add S-PIN to your configuration or .netrc file")
                return False
            spin = self.active_config["spin"]
        url = self.base_url + f"/ss/v1/user/{self.session.backend_user_id}/spin"
        payload = {"spin": spin}
        try:
            result = self.session.post(url, data=json.dumps(payload), allow_redirects=True, access_type=AccessType.ACCESS)
            print(str(result.text))
            return True
        except HTTPError as http_error:
            LOG.error(f"Could not set SPIN token, error was: {http_error.response.text if http_error.response is not None else str(http_error)}")
            return False

    def __do_spin(self, vehicle: VolkswagenNAVehicle, spin: str | None = None) -> str | None:  # pylint: disable=unused-private-member
        if not isinstance(vehicle, VolkswagenNAVehicle):
            raise CommandError("Object is not a VolkswagenNAVehicle")
        # Some accounts/regions (notably myVW Canada) report SPIN:ALL capability with
        # status NOT_APPLICABLE, but SPIN actually works for these vehicles.
        # Log it as informational and proceed with the SPIN flow regardless.
        # See: https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na/issues/66
        if vehicle.capabilities.has_capability("SPIN:ALL"):
            spin_capability = vehicle.capabilities.get_capability("SPIN:ALL")
            if (
                spin_capability is not None
                and spin_capability.status.enabled
                and spin_capability.status.value is not None
                and Capability.Status.NOT_APPLICABLE in spin_capability.status.value
            ):
                LOG.info("SPIN:ALL capability reports NOT_APPLICABLE for vehicle %s, but proceeding with SPIN challenge anyway", vehicle.vin)
        LOG.debug("Checking for cached spin token: %s, expires at %s", vehicle.spin_token, vehicle.spin_token_expiry)
        # Use a 120-second buffer for SPIN token expiry to match the access token buffer
        spin_expiry_buffer = timedelta(seconds=120)
        if (
            vehicle.spin_token is not None
            and vehicle.spin_token_expiry is not None
            and vehicle.spin_token_expiry > (datetime.now(timezone.utc) + spin_expiry_buffer)
        ):
            LOG.debug("Using cached SPIN token, expires at %s", vehicle.spin_token_expiry.isoformat())
            return vehicle.spin_token
        if spin is None:
            if self.active_config["spin"] is None or self.active_config["spin"] == "":
                LOG.warning("S-PIN is missing, please add S-PIN to your configuration or .netrc file")
                return None
            spin = self.active_config["spin"]
        challenge_url = self.base_url + f"/ss/v1/user/{self.session.backend_user_id}/challenge"
        verify_url = self.base_url + f"/ss/v1/user/{self.session.backend_user_id}/vehicle/{vehicle.uuid.value}/session"
        try:
            try:
                challenge_response: requests.Response = self.session.get(challenge_url, access_type=AccessType.ACCESS)
            except HTTPError as http_error:
                http_status = _get_http_status_code(http_error)
                LOG.debug("HTTPError in __do_spin challenge: status_code=%s, response=%s, err=%s", http_status, http_error.response, str(http_error))
                if http_status == requests.codes["unauthorized"]:
                    LOG.warning("Got 401 on SPIN challenge, refreshing auth and retrying")
                    try:
                        self.session.refresh()
                    except (AuthenticationError, Exception):
                        self.session.login()
                    try:
                        challenge_response = self.session.get(challenge_url, access_type=AccessType.ACCESS)
                    except HTTPError as retry_error:
                        LOG.error("SPIN challenge retry also failed: %s", str(retry_error))
                        return None
                elif http_status == requests.codes["not_found"]:
                    if self.active_config["set_spin"] is not None and self.active_config["set_spin"] is True:
                        resp_text = http_error.response.text if http_error.response is not None else "no response body"
                        LOG.warning("SPIN challenge endpoint not found, but set_spin is enabled, trying to set SPIN. Error was: " + resp_text)
                        if not self.__do_set_spin(vehicle, spin):
                            return None
                        challenge_response = self.session.get(challenge_url, access_type=AccessType.ACCESS)
                    else:
                        resp_text = http_error.response.text if http_error.response is not None else "no response body"
                        LOG.warning("SPIN challenge endpoint not found: " + resp_text)
                        return None
                elif http_status == requests.codes["forbidden"]:
                    if self.active_config["set_spin"] is not None and self.active_config["set_spin"] is True:
                        resp_text = http_error.response.text if http_error.response is not None else "no response body"
                        LOG.warning("SPIN challenge endpoint forbidden, but set_spin is enabled, trying to set SPIN. Error was: " + resp_text)
                        if not self.__do_set_spin(vehicle, spin):
                            return None
                        challenge_response = self.session.get(challenge_url, access_type=AccessType.ACCESS)
                    else:
                        resp_text = http_error.response.text if http_error.response is not None else "no response body"
                        LOG.warning("SPIN challenge endpoint forbidden: " + resp_text)
                        return None
                else:
                    raise http_error
            challenge_response_data = challenge_response.json()
            challenge_string = challenge_response_data["data"]["challenge"]
            if challenge_response_data["data"]["remainingTries"] < 3:
                LOG.warning(f"Skipping SPIN token fetching, only {challenge_response_data['data']['remainingTries']} tries remaining")
                return None
            verify_string = challenge_string + "." + spin
            verify_hash = hashlib.sha512(verify_string.encode("utf-8")).hexdigest().upper()
            # Use country-specific TSP
            tsp = "WCT"
            verify_data = {"idToken": self.session.id_token, "spinHash": verify_hash, "tsp": tsp}
            verify_response: requests.Response = self.session.post(
                verify_url, data=json.dumps(verify_data), allow_redirects=True, access_type=AccessType.ACCESS
            )
            if verify_response.status_code != requests.codes["ok"]:
                LOG.error("Could not execute spin verify (%s: %s)", verify_response.status_code, verify_response.text)
                return None
                # raise CommandError(f"Could not execute spin verify ({verify_response.status_code}: {verify_response.text})")
            else:
                LOG.debug("Spin verify command executed successfully")
                vehicle.spin_token = verify_response.json()["data"]["carnetVehicleToken"]
                if vehicle.spin_token is not None:
                    data = jwt.decode(vehicle.spin_token, options={"verify_signature": False})
                    vehicle.spin_token_expiry = datetime.fromtimestamp(data["exp"], tz=timezone.utc)
                    LOG.debug("Fetched new SPIN token, expires at %s", vehicle.spin_token_expiry.isoformat())
                return verify_response.json()["data"]["carnetVehicleToken"]
        except requests.exceptions.ConnectionError as connection_error:
            raise CommandError(
                f"Connection error: {connection_error}. If this happens frequently, please check if other applications communicate with the Volkswagen server."
            ) from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise CommandError(f"Error: {chunked_encoding_error}") from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise CommandError(f"Timeout during read: {timeout_error}") from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise CommandError(f"Retrying failed: {retry_error}") from retry_error

    def __on_charging_start_stop(
        self, start_stop_command: ChargingStartStopCommand, command_arguments: Union[str, Dict[str, Any]]
    ) -> Union[str, Dict[str, Any]]:
        if (
            start_stop_command.parent is None
            or start_stop_command.parent.parent is None
            or start_stop_command.parent.parent.parent is None
            or not isinstance(start_stop_command.parent.parent.parent, VolkswagenNAVehicle)
        ):
            raise CommandError("Object hierarchy is not as expected")
        if not isinstance(command_arguments, dict):
            raise CommandError("Command arguments are not a dictionary")
        vehicle: VolkswagenNAVehicle = start_stop_command.parent.parent.parent
        vuuid: Optional[str] = vehicle.uuid.value
        if vuuid is None:
            raise CommandError("VUUID in object hierarchy missing")
        if "command" not in command_arguments:
            raise CommandError("Command argument missing")
        try:
            token = self.__do_spin(vehicle)
        except HTTPError as http_error:
            LOG.info(f"Could not fetch SPIN token, trying to execute charging command without token. Error was: {http_error}")
            token = None
        try:
            if command_arguments["command"] == ChargingStartStopCommand.Command.START:
                url = self.base_url + f"/ev/v1/vehicle/{vuuid}/charging/start"
                charging_request = {"actionMode": "immediate"}
                command_response: requests.Response = self.session.post(url, data=json.dumps(charging_request), allow_redirects=True, token=token)
            elif command_arguments["command"] == ChargingStartStopCommand.Command.STOP:
                url = self.base_url + f"/ev/v1/vehicle/{vuuid}/charging/stop"
                command_response: requests.Response = self.session.post(url, data="{}", allow_redirects=True, token=token)
            else:
                raise CommandError(f"Unknown command {command_arguments['command']}")

            if command_response.status_code != requests.codes["ok"]:
                LOG.error("Could not start/stop charging (%s: %s)", command_response.status_code, command_response.text)
                raise CommandError(f"Could not start/stop charging ({command_response.status_code}: {command_response.text})")
            # Optimistic state update: immediately reflect the expected charging state
            # so the UI doesn't show stale data until the next poll (up to 600s)
            optimistic_time = datetime.now(tz=timezone.utc)
            if command_arguments["command"] == ChargingStartStopCommand.Command.START:
                self.update_enum(vehicle.charging.state, Charging.ChargingState.CHARGING, optimistic_time)
            elif command_arguments["command"] == ChargingStartStopCommand.Command.STOP:
                self.update_enum(vehicle.charging.state, Charging.ChargingState.OFF, optimistic_time)
            LOG.debug("Optimistic state update: charging state set to %s", vehicle.charging.state.value)
        except requests.exceptions.ConnectionError as connection_error:
            raise CommandError(
                f"Connection error: {connection_error}. If this happens frequently, please check if other applications communicate with the Volkswagen server."
            ) from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise CommandError(f"Error: {chunked_encoding_error}") from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise CommandError(f"Timeout during read: {timeout_error}") from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise CommandError(f"Retrying failed: {retry_error}") from retry_error
        return command_arguments

    def __on_charging_settings_change(self, attribute: GenericAttribute, value: Any) -> Any:
        """
        Callback for the charging setting change.
        """
        if (
            attribute.parent is None
            or not isinstance(attribute.parent, VolkswagenNACharging.Settings)
            or attribute.parent.parent is None
            or attribute.parent.parent.parent is None
            or not isinstance(attribute.parent.parent.parent, VolkswagenNAVehicle)
        ):
            raise SetterError("Object hierarchy is not as expected")
        settings: VolkswagenNACharging.Settings = attribute.parent
        vehicle: VolkswagenNAVehicle = attribute.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise SetterError("VIN in object hierarchy missing")
        vuuid: Optional[str] = vehicle.uuid.value
        if vuuid is None:
            raise SetterError("UUID in object hierarchy missing")
        setting_dict = {}
        precision: float = settings.maximum_current.precision if settings.maximum_current.precision is not None else 1.0
        if isinstance(attribute, CurrentAttribute) and attribute.id == "maximum_current":
            value = round(value / precision) * precision
            if value < 12:
                setting_dict["maxChargingCurrent"] = "reduced"
                value = 10.0
            else:
                setting_dict["maxChargingCurrent"] = "max"
                value = 32.0
        if isinstance(attribute, BooleanAttribute) and attribute.id == "auto_unlock":
            setting_dict["autoUnlockPlugWhenCharged"] = "on" if value else "off"
        precision: float = settings.target_level.precision if settings.target_level.precision is not None else 10.0
        if isinstance(attribute, LevelAttribute) and attribute.id == "target_level":
            value = round(value / precision) * precision
            setting_dict["targetSOCPercentage"] = round(value / precision) * precision

        url: str = self.base_url + f"/ev/v1/vehicle/{vuuid}/charging/settings"
        try:
            token = self.__do_spin(vehicle)
        except HTTPError as http_error:
            LOG.info(f"Could not fetch SPIN token, trying to execute charging settings change without token. Error was: {http_error}")
            token = None

        try:
            settings_response: requests.Response = self.session.put(url, data=json.dumps(setting_dict), allow_redirects=True, token=token)
            if settings_response.status_code != requests.codes["ok"]:
                LOG.error("Could not set charging settings (%s)", settings_response.status_code)
                raise SetterError(f"Could not set value ({settings_response.status_code})")
        except requests.exceptions.ConnectionError as connection_error:
            raise SetterError(
                f"Connection error: {connection_error}. If this happens frequently, please check if other applications communicate with the Volkswagen server."
            ) from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise SetterError(f"Error: {chunked_encoding_error}") from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise SetterError(f"Timeout during read: {timeout_error}") from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise SetterError(f"Retrying failed: {retry_error}") from retry_error
        except HTTPError as http_error:
            raise SetterError(f"HTTP error setting charging settings: {http_error}") from http_error
        return value

    def __on_window_heating_start_stop(
        self, start_stop_command: WindowHeatingStartStopCommand, command_arguments: Union[str, Dict[str, Any]]
    ) -> Union[str, Dict[str, Any]]:
        if (
            start_stop_command.parent is None
            or start_stop_command.parent.parent is None
            or start_stop_command.parent.parent.parent is None
            or not isinstance(start_stop_command.parent.parent.parent, VolkswagenNAVehicle)
        ):
            raise CommandError("Object hierarchy is not as expected")
        if not isinstance(command_arguments, dict):
            raise CommandError("Command arguments are not a dictionary")
        vehicle: VolkswagenNAVehicle = start_stop_command.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError("VIN in object hierarchy missing")
        vuuid: Optional[str] = vehicle.uuid.value
        if vuuid is None:
            raise SetterError("UUID in object hierarchy missing")
        if "command" not in command_arguments:
            raise CommandError("Command argument missing")
        try:
            token = self.__do_spin(vehicle)
        except HTTPError as http_error:
            LOG.info(f"Could not fetch SPIN token, trying to execute window heating command without token. Error was: {http_error}")
            token = None

        try:
            if command_arguments["command"] == WindowHeatingStartStopCommand.Command.START:
                url = self.base_url + "/ev/v1/vehicle/{vuuid}/pretripclimate/windowheating/start"
                command_response: requests.Response = self.session.post(url, data="{}", allow_redirects=True, token=token)
            elif command_arguments["command"] == WindowHeatingStartStopCommand.Command.STOP:
                url = self.base_url + "/ev/v1/vehicle/{vuuid}/pretripclimate/windowheating/stop"
                command_response: requests.Response = self.session.post(url, data="{}", allow_redirects=True, token=token)
            else:
                raise CommandError(f"Unknown command {command_arguments['command']}")

            if command_response.status_code != requests.codes["ok"]:
                LOG.error("Could not start/stop window heating (%s: %s)", command_response.status_code, command_response.text)
                raise CommandError(f"Could not start/stop window heating ({command_response.status_code}: {command_response.text})")
        except requests.exceptions.ConnectionError as connection_error:
            raise CommandError(
                f"Connection error: {connection_error}. If this happens frequently, please check if other applications communicate with the Volkswagen server."
            ) from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise CommandError(f"Error: {chunked_encoding_error}") from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise CommandError(f"Timeout during read: {timeout_error}") from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise CommandError(f"Retrying failed: {retry_error}") from retry_error
        return command_arguments

    def get_name(self) -> str:
        return "Volkswagen NA (MyVW) Connector"
