"""Module for vehicle classes."""
from __future__ import annotations
from typing import TYPE_CHECKING

from carconnectivity.vehicle import GenericVehicle, ElectricVehicle, CombustionVehicle, HybridVehicle

if TYPE_CHECKING:
    from typing import Optional
    from carconnectivity.garage import Garage
    from carconnectivity_connectors.volkswagen.capability import Capability
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
        super().__init__(vin=vin, garage=garage, origin=origin)
        if origin is not None:
            super().__init__(vin=vin, garage=garage, origin=origin, managing_connector=managing_connector)
            self.capabilities = origin.capabilities
        else:
            self.capabilities: dict[str, Capability] = {}

    def __str__(self) -> str:
        return_string: str = super().__str__()
        if self.capabilities is not None and len(self.capabilities) > 0:
            return_string += 'Capabilities:\n'
            for capability in self.capabilities.values():
                return_string += f'\t{capability}\n'
        return return_string


class VolkswagenElectricVehicle(ElectricVehicle, VolkswagenVehicle):
    """
    Represents a Volkswagen electric vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[GenericVehicle] = None) -> None:
        if origin is not None:
            super().__init__(origin=origin)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)


class VolkswagenCombustionVehicle(CombustionVehicle, VolkswagenVehicle):
    """
    Represents a Volkswagen combustion vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[GenericVehicle] = None) -> None:
        if origin is not None:
            super().__init__(origin=origin)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)


class VolkswagenHybridVehicle(HybridVehicle, VolkswagenVehicle):
    """
    Represents a Volkswagen hybrid vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[GenericVehicle] = None) -> None:
        if origin is not None:
            super().__init__(origin=origin)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)
