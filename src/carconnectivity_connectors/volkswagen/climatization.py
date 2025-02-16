"""
Module for charging for skoda vehicles.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

from carconnectivity.climatization import Climatization
from carconnectivity.objects import GenericObject
from carconnectivity.vehicle import ElectricVehicle
from carconnectivity.attributes import BooleanAttribute
from carconnectivity.units import Temperature

if TYPE_CHECKING:
    from typing import Optional


class VolkswagenClimatization(Climatization):  # pylint: disable=too-many-instance-attributes
    """
    VolkswagenClimatization class for handling Volkswagen vehicle climatization information.

    This class extends the Climatization class and includes an enumeration of various
    climatization states specific to Volkswagen vehicles.
    """
    def __init__(self, vehicle: ElectricVehicle | None = None, origin: Optional[Climatization] = None) -> None:
        if origin is not None:
            super().__init__(origin=origin)
            self.settings: Climatization.Settings = VolkswagenClimatization.Settings(origin=origin.settings)
        else:
            super().__init__(vehicle=vehicle)
            self.settings: Climatization.Settings = VolkswagenClimatization.Settings(origin=self.settings)

    class Settings(Climatization.Settings):
        """
        This class represents the settings for a skoda car climatiation.
        """
        def __init__(self, parent: Optional[GenericObject] = None, origin: Optional[Climatization.Settings] = None) -> None:
            if origin is not None:
                super().__init__(origin=origin)
            else:
                super().__init__(parent=parent)
            self.unit_in_car: Optional[Temperature] = None
            self.front_zone_left_enabled: BooleanAttribute = BooleanAttribute(parent=self, name='front_zone_left_enabled', tags={'connector_custom'})
            self.front_zone_right_enabled: BooleanAttribute = BooleanAttribute(parent=self, name='front_zone_right_enabled', tags={'connector_custom'})
            self.rear_zone_left_enabled: BooleanAttribute = BooleanAttribute(parent=self, name='rear_zone_left_enabled', tags={'connector_custom'})
            self.rear_zone_right_enabled: BooleanAttribute = BooleanAttribute(parent=self, name='rear_zone_right_enabled', tags={'connector_custom'})
