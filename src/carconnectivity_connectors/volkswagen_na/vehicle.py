"""Module for vehicle classes."""
from __future__ import annotations
from typing import TYPE_CHECKING

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
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[VolkswagenVehicle] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin)
            self.capabilities: Capabilities = origin.capabilities
            self.capabilities.parent = self
            self.is_active: BooleanAttribute = origin.is_active
            self.is_active.parent = self
            self.uuid: StringAttribute = origin.uuid
            self.uuid.parent = self
            self.spin_token = origin.spin_token
            self.spin_token.parent = self
            if SUPPORT_IMAGES:
                self._car_images = origin._car_images
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)
            self.capabilities: Capabilities = Capabilities(vehicle=self)
            self.climatization = VolkswagenClimatization(vehicle=self, origin=self.climatization)
            self.is_active = BooleanAttribute(name='is_active', parent=self, tags={'connector_custom'})
            self.uuid = StringAttribute('uuid', self, tags={'connector_custom'})
            self.spin_token = StringAttribute('spin_token', self, tags={'connector_custom'})
            if SUPPORT_IMAGES:
                self._car_images: Dict[str, Image.Image] = {}
        self.manufacturer._set_value(value='Volkswagen')  # pylint: disable=protected-access


class VolkswagenNAElectricVehicle(ElectricVehicle, VolkswagenNAVehicle):
    """
    Represents a Volkswagen electric vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[VolkswagenVehicle] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin)
            if isinstance(origin, ElectricVehicle):
                self.charging = VolkswagenNACharging(vehicle=self, origin=origin.charging)
            else:
                self.charging = VolkswagenNACharging(vehicle=self, origin=self.charging)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)
            self.charging = VolkswagenNACharging(vehicle=self, origin=self.charging)


class VolkswagenNACombustionVehicle(CombustionVehicle, VolkswagenNAVehicle):
    """
    Represents a Volkswagen combustion vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[VolkswagenVehicle] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)


class VolkswagenNAHybridVehicle(HybridVehicle, VolkswagenNAElectricVehicle, VolkswagenNACombustionVehicle):
    """
    Represents a Volkswagen hybrid vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[VolkswagenVehicle] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)
