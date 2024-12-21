"""Module implements the connector to interact with the Skoda API."""
from __future__ import annotations
from typing import TYPE_CHECKING

import os
import logging
import netrc
from datetime import datetime, timedelta
import requests

from carconnectivity.vehicle import GenericVehicle
from carconnectivity.garage import Garage
from carconnectivity.errors import AuthenticationError, TooManyRequestsError, RetrievalError
from carconnectivity_connectors.base.connector import BaseConnector
from carconnectivity_connectors.volkswagen.auth.session_manager import SessionManager, SessionUser, Service


if TYPE_CHECKING:
    from typing import Dict, List, Optional, Any

    from requests import Session

    from carconnectivity.carconnectivity import CarConnectivity

LOG: logging.Logger = logging.getLogger("carconnectivity-connector-skoda")


class Connector(BaseConnector):
    """
    Connector class for Skoda API connectivity.
    Args:
        car_connectivity (CarConnectivity): An instance of CarConnectivity.
        config (Dict): Configuration dictionary containing connection details.
    Attributes:
        max_age (Optional[int]): Maximum age for cached data in seconds.
    """
    def __init__(self, car_connectivity: CarConnectivity, config: Dict) -> None:
        BaseConnector.__init__(self, car_connectivity, config)
        LOG.info("Loading skoda connector with config %s", self.config)

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
                    raise AuthenticationError(f'skoda.de entry was not found in {netrc_filename} netrc-file.'
                                              ' Create it or provide username and password in config') from err
            except FileNotFoundError as err:
                raise AuthenticationError(f'{netrc_filename} netrc-file was not found. Create it or provide username and password in config') from err

        self.max_age: Optional[int] = 300
        if 'maxAge' in self.config:
            self.max_age = self.config['maxAge']

        if username is None or password is None:
            raise AuthenticationError('Username or password not provided')

        self._manager: SessionManager = SessionManager(tokenstore=car_connectivity.get_tokenstore(), cache=car_connectivity.get_cache())
        self._session: Session = self._manager.get_session(Service.WE_CONNECT, SessionUser(username=username, password=password))

        self._elapsed: List[timedelta] = []

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
        self.persist()
        self._session.close()
        BaseConnector.shutdown(self)

    def fetch_all(self) -> None:
        """
        Fetches all necessary data for the connector.

        This method calls the `fetch_vehicles` method to retrieve vehicle data.
        """
        self.fetch_vehicles()

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

        if data is not None:
            if 'data' in data and data['data'] is not None:
                for vehicle_dict in data['data']:
                    if 'vin' in vehicle_dict and vehicle_dict['vin'] is not None:
                        vehicle: Optional[GenericVehicle] = garage.get_vehicle(vehicle_dict['vin'])
                        if vehicle is None:
                            vehicle = GenericVehicle(vin=vehicle_dict['vin'], garage=garage)
                            garage.add_vehicle(vehicle_dict['vin'], vehicle)

                        if 'nickname' in vehicle_dict and vehicle_dict['nickname'] is not None:
                            vehicle.name._set_value(vehicle_dict['nickname'])  # pylint: disable=protected-access

                        if 'model' in vehicle_dict and vehicle_dict['model'] is not None:
                            vehicle.model._set_value(vehicle_dict['model'])  # pylint: disable=protected-access

                        self.fetch_vehicle_status(vehicle)

    def fetch_vehicle_status(self, vehicle: GenericVehicle) -> None:
        """
        Fetches the status of a vehicle from the Volkswagen API.

        Args:
            vehicle (GenericVehicle): The vehicle object containing the VIN.

        Returns:
            None
        """
        vin = vehicle.vin.value
        url = f'https://emea.bff.cariad.digital/vehicle/v1/vehicles/{vin}/selectivestatus?jobs=measurements'
        data: Dict[str, Any] | None = self._fetch_data(url, self._session)
        print(data)

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
                raise RetrievalError from connection_error
            except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
                raise RetrievalError from chunked_encoding_error
            except requests.exceptions.ReadTimeout as timeout_error:
                raise RetrievalError from timeout_error
            except requests.exceptions.RetryError as retry_error:
                raise RetrievalError from retry_error
            except requests.exceptions.JSONDecodeError as json_error:
                if allow_empty:
                    data = None
                else:
                    raise RetrievalError from json_error
        return data
