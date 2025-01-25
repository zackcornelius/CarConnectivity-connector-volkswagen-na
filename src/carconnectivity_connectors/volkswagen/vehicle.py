"""Module for vehicle classes."""
from __future__ import annotations
from typing import TYPE_CHECKING

from carconnectivity.vehicle import GenericVehicle, ElectricVehicle, CombustionVehicle, HybridVehicle

from carconnectivity_connectors.volkswagen.capability import Capabilities

if TYPE_CHECKING:
    from typing import Optional
    from carconnectivity.garage import Garage
    from carconnectivity_connectors.base.connector import BaseConnector


class VolkswagenVehicle(GenericVehicle):  # pylint: disable=too-many-instance-attributes
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
            super().__init__(origin=origin)
            self.capabilities: Capabilities = origin.capabilities
            self.capabilities.parent = self
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)
            self.capabilities: Capabilities = Capabilities(vehicle=self)
        self.manufacturer._set_value(value='Volkswagen')  # pylint: disable=protected-access


class VolkswagenElectricVehicle(ElectricVehicle, VolkswagenVehicle):
    """
    Represents a Volkswagen electric vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[VolkswagenVehicle] = None) -> None:
        if origin is not None:
            super().__init__(origin=origin)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)


class VolkswagenCombustionVehicle(CombustionVehicle, VolkswagenVehicle):
    """
    Represents a Volkswagen combustion vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[VolkswagenVehicle] = None) -> None:
        if origin is not None:
            super().__init__(origin=origin)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)


class VolkswagenHybridVehicle(HybridVehicle, VolkswagenVehicle):
    """
    Represents a Volkswagen hybrid vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[VolkswagenVehicle] = None) -> None:
        if origin is not None:
            super().__init__(origin=origin)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)
