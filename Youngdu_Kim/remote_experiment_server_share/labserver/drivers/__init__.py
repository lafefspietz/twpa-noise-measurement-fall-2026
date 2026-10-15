from .base import Instrument, InstrumentError, MeasurementError, Trace
from .e7405a import E7405A
from .fgen33220a import Fgen33220A
from .gs210 import GS210
from .rc4spdt import RC4SPDT
from .rcdat import RCDAT
from .windfreak import WindfreakSynthHD
from .zva import ZVA

# config.yaml `driver:` name -> driver class
DRIVERS: dict[str, type[Instrument]] = {
    "e7405a": E7405A,
    "zva": ZVA,
    "windfreak": WindfreakSynthHD,
    "gs210": GS210,
    "fgen33220a": Fgen33220A,
    "rc4spdt": RC4SPDT,
    "rcdat": RCDAT,
}

__all__ = ["DRIVERS", "E7405A", "Fgen33220A", "GS210", "RC4SPDT", "RCDAT", "WindfreakSynthHD", "ZVA",
           "Instrument", "InstrumentError", "MeasurementError", "Trace"]
