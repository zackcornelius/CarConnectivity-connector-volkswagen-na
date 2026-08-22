"""Module for vehicle classes."""

from __future__ import annotations
from typing import TYPE_CHECKING
from datetime import datetime

from carconnectivity.vehicle import GenericVehicle, ElectricVehicle, CombustionVehicle, HybridVehicle
from carconnectivity.attributes import StringAttribute
from carconnectivity.attributes import BooleanAttribute

from carconnectivity_connectors.volkswagen_na.capability import Capabilities
from carconnectivity_connectors.volkswagen_na.climatization import VolkswagenClimatization
from carconnectivity_connectors.volkswagen_na.charging import VolkswagenNACharging

SUPPORT_IMAGES = False
try:
    from PIL import Image

    SUPPORT_IMAGES = True
except ImportError:
    pass

if TYPE_CHECKING:
    from typing import Optional, Dict
    from carconnectivity.garage import Garage
    from carconnectivity_connectors.base.connector import BaseConnector


class VolkswagenNAVehicle(GenericVehicle):  # pylint: disable=too-many-instance-attributes
    """
    A class to represent a generic volkswagen vehicle.

    Attributes:
    -----------
    vin : StringAttribute
        The vehicle identification number (VIN) of the vehicle.
    license_plate : StringAttribute
        The license plate of the vehicle.
    """

    spin_token: Optional[str] = None
    spin_token_expiry: Optional[datetime] = None
    tsp: Optional[str] = None  # Telematics Service Provider: "WCT" (WirelessCar/MEB) or "ATC" (Aeris/ICE)

    def __init__(
        self,
        vin: Optional[str] = None,
        garage: Optional[Garage] = None,
        managing_connector: Optional[BaseConnector] = None,
        origin: Optional[VolkswagenNAVehicle] = None,
        initialization: Optional[Dict] = None,
    ) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin, initialization=initialization)
            self.capabilities: Capabilities = origin.capabilities
            self.capabilities.parent = self
            self.is_active: BooleanAttribute = origin.is_active
            self.is_active.parent = self
            self.uuid: StringAttribute = origin.uuid
            self.uuid.parent = self
            self.spin_token = origin.spin_token
            self.spin_token_expiry = origin.spin_token_expiry
            self.tsp = origin.tsp
            if SUPPORT_IMAGES:
                self._car_images = origin._car_images
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)
            self.capabilities: Capabilities = Capabilities(vehicle=self, initialization=self.get_initialization("capabilities"))
            self.climatization = VolkswagenClimatization(vehicle=self, origin=self.climatization, initialization=self.get_initialization("climatization"))
            self.is_active = BooleanAttribute(name="is_active", parent=self, tags={"connector_custom"}, initialization=self.get_initialization("is_active"))
            self.uuid = StringAttribute("uuid", self, tags={"connector_custom"}, initialization=self.get_initialization("uuid"))
            if SUPPORT_IMAGES:
                self._car_images: Dict[str, Image.Image] = {}
        self.manufacturer._set_value(value="Volkswagen")  # pylint: disable=protected-access


class VolkswagenNAElectricVehicle(ElectricVehicle, VolkswagenNAVehicle):
    """
    Represents a Volkswagen electric vehicle.
    """

    def __init__(
        self,
        vin: Optional[str] = None,
        garage: Optional[Garage] = None,
        managing_connector: Optional[BaseConnector] = None,
        origin: Optional[VolkswagenNAVehicle] = None,
        initialization: Optional[Dict] = None,
    ) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin, initialization=initialization)
            if isinstance(origin, ElectricVehicle):
                self.charging = VolkswagenNACharging(vehicle=self, origin=origin.charging)
            else:
                self.charging = VolkswagenNACharging(vehicle=self, origin=self.charging)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)
            self.charging = VolkswagenNACharging(vehicle=self, initialization=self.get_initialization("charging"))


class VolkswagenNACombustionVehicle(CombustionVehicle, VolkswagenNAVehicle):
    """
    Represents a Volkswagen combustion vehicle.
    """

    def __init__(
        self,
        vin: Optional[str] = None,
        garage: Optional[Garage] = None,
        managing_connector: Optional[BaseConnector] = None,
        origin: Optional[VolkswagenNAVehicle] = None,
        initialization: Optional[Dict] = None,
    ) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin, initialization=initialization)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)


class VolkswagenNAHybridVehicle(HybridVehicle, VolkswagenNAElectricVehicle, VolkswagenNACombustionVehicle):
    """
    Represents a Volkswagen hybrid vehicle.
    """

    def __init__(
        self,
        vin: Optional[str] = None,
        garage: Optional[Garage] = None,
        managing_connector: Optional[BaseConnector] = None,
        origin: Optional[VolkswagenNAVehicle] = None,
        initialization: Optional[Dict] = None,
    ) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin, initialization=initialization)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)
