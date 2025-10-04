"""
Module for charging for Volskwagen vehicles.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

from enum import Enum

from carconnectivity.charging import Charging
from carconnectivity.vehicle import ElectricVehicle

if TYPE_CHECKING:
    from typing import Optional, Dict

    from carconnectivity.objects import GenericObject


class VolkswagenNACharging(Charging):  # pylint: disable=too-many-instance-attributes
    """
    VolkswagenNACharging class for handling Volkswagen vehicle charging information.

    This class extends the Charging class and includes an enumeration of various
    charging states specific to Volkswagen vehicles.
    """
    def __init__(self, vehicle: ElectricVehicle | None = None, origin: Optional[Charging] = None) -> None:
        if origin is not None:
            super().__init__(vehicle=vehicle, origin=origin)
            self.settings = VolkswagenNACharging.Settings(parent=self, origin=origin.settings)
        else:
            super().__init__(vehicle=vehicle)
            self.settings = VolkswagenNACharging.Settings(parent=self, origin=self.settings)

    class Settings(Charging.Settings):
        """
        This class represents the settings for car volkswagen car charging.
        """
        def __init__(self, parent: Optional[GenericObject] = None, origin: Optional[Charging.Settings] = None) -> None:
            if origin is not None:
                super().__init__(parent=parent, origin=origin)
            else:
                super().__init__(parent=parent)
            self.max_current_in_ampere: Optional[bool] = None

    class VolkswagenChargingState(Enum,):
        """
        Enum representing the various charging states for a Volkswagen vehicle.
        """
        OFF = 'off'
        READY_FOR_CHARGING = 'readyForCharging'
        NOT_READY_FOR_CHARGING = 'notReadyForCharging'
        CONSERVATION = 'conservation'
        CHARGE_PURPOSE_REACHED_NOT_CONSERVATION_CHARGING = 'chargePurposeReachedAndNotConservationCharging'
        CHARGE_PURPOSE_REACHED_CONSERVATION = 'chargePurposeReachedAndConservation'
        CHARGING = 'chargingHVBattery'
        ERROR = 'error'
        UNSUPPORTED = 'unsupported'
        DISCHARGING = 'discharging'
        UNKNOWN = 'unknown charging state'

    class VolkswagenChargeMode(Enum,):
        """
        Enum class representing different Volkswagen charge modes.
        """
        MANUAL = 'manual'
        INVALID = 'invalid'
        OFF = 'off'
        TIMER = 'timer'
        ONLY_OWN_CURRENT = 'onlyOwnCurrent'
        PREFERRED_CHARGING_TIMES = 'preferredChargingTimes'
        TIMER_CHARGING_WITH_CLIMATISATION = 'timerChargingWithClimatisation'
        HOME_STORAGE_CHARGING = 'homeStorageCharging'
        IMMEDIATE_DISCHARGING = 'immediateDischarging'
        UNKNOWN = 'unknown charge mode'


# Mapping of Volkswagen charging states to generic charging states
mapping_volskwagen_charging_state: Dict[VolkswagenNACharging.VolkswagenChargingState, Charging.ChargingState] = {
    VolkswagenNACharging.VolkswagenChargingState.OFF: Charging.ChargingState.OFF,
    VolkswagenNACharging.VolkswagenChargingState.NOT_READY_FOR_CHARGING: Charging.ChargingState.OFF,
    VolkswagenNACharging.VolkswagenChargingState.READY_FOR_CHARGING: Charging.ChargingState.READY_FOR_CHARGING,
    VolkswagenNACharging.VolkswagenChargingState.CONSERVATION: Charging.ChargingState.CONSERVATION,
    VolkswagenNACharging.VolkswagenChargingState.CHARGE_PURPOSE_REACHED_NOT_CONSERVATION_CHARGING: Charging.ChargingState.READY_FOR_CHARGING,
    VolkswagenNACharging.VolkswagenChargingState.CHARGE_PURPOSE_REACHED_CONSERVATION: Charging.ChargingState.CONSERVATION,
    VolkswagenNACharging.VolkswagenChargingState.CHARGING: Charging.ChargingState.CHARGING,
    VolkswagenNACharging.VolkswagenChargingState.ERROR: Charging.ChargingState.ERROR,
    VolkswagenNACharging.VolkswagenChargingState.UNSUPPORTED: Charging.ChargingState.UNSUPPORTED,
    VolkswagenNACharging.VolkswagenChargingState.DISCHARGING: Charging.ChargingState.DISCHARGING,
    VolkswagenNACharging.VolkswagenChargingState.UNKNOWN: Charging.ChargingState.UNKNOWN
}
