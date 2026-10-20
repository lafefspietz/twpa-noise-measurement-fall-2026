"""Rohde & Schwarz SMW200A vector signal generator, used as a CW source (the probe / signal tone).

GPIB through its own GpibUsb adapter, plain SCPI. The lab's unit has one RF path (A,
SMW-B1044: 100 kHz-44 GHz, -145..+30 dBm settable); SOURce2/OUTPut2 are not fitted.
Parameters are flattened to "<role>_<param>" like the SynthHD channels (probe_frequency_hz,
probe_power_dbm, probe_enable), so the experiments and the saved records keep their keys;
`channels` in config.yaml maps the role to the path number (probe: 1 = SOURce1/OUTPut1).

Only the CW frequency, the level and RF on/off are ever set. Modulation (MOD:STAT, IQ),
the frequency/level modes, the offsets and the reference oscillator are read and shown
but never changed: set them on the front panel. Frequency and level are the values the
instrument displays, i.e. with its frequency/level offset (both 0 when first connected,
2026-10-19).

Like the E7405A, the SMW does not answer an unsupported query (the read times out and
-113/-241 goes to the error queue), so only queries proven on the instrument are used.
After every exchange the front panel is handed back (USB488 GO_TO_LOCAL); the next
command takes it to remote again.
"""
from __future__ import annotations

from ..sim import SimTransport, scpi
from ..sim.scpi import parse_bool, parse_number
from ..transport import TransportError
from .base import Instrument, InstrumentError, fmt

FREQ_RANGE_HZ = (100e3, 44e9)        # SMW-B1044, :SOUR1:FREQ:CW? MIN/MAX
POWER_RANGE_DBM = (-145.0, 30.0)     # :SOUR1:POW:LEV:IMM:AMPL? MIN/MAX (above ~+18 dBm is overrange)
DEFAULT_CHANNELS = {"probe": 1}
PARAMS = ("enable", "frequency_hz", "power_dbm")


class SMW200A(Instrument):
    kind = "rf_source"

    @classmethod
    def limit_specs(cls, cfg):
        return [spec for ch in cfg.get("channels", DEFAULT_CHANNELS)
                for spec in ((f"{ch}_frequency_hz", f"{ch} frequency", "GHz", 1e9),
                             (f"{ch}_power_dbm", f"{ch} power", "dBm", 1))]

    def __init__(self, name, transport, cfg=None):
        super().__init__(name, transport, cfg)
        self.channels = self.cfg.get("channels", DEFAULT_CHANNELS)
        if any("_" in ch for ch in self.channels):
            raise InstrumentError(f"{name}: channel names must not contain '_': {list(self.channels)}")

    def settable(self) -> list[str]:
        return [f"{ch}_{p}" for ch in self.channels for p in PARAMS]

    def apply(self, **params) -> dict:
        # RF off goes first, RF on last, so a new frequency/level is never on the output
        # before it is set
        def rank(key):
            if key.endswith("_enable"):
                return 2 if params[key] else 0
            return 1
        return super().apply(**dict(sorted(params.items(), key=lambda kv: rank(kv[0]))))

    def check_limit(self, key, value):
        super().check_limit(key, value)
        param = key.partition("_")[2]
        rng = {"frequency_hz": FREQ_RANGE_HZ, "power_dbm": POWER_RANGE_DBM}.get(param)
        if rng and not rng[0] <= float(value) <= rng[1]:
            raise InstrumentError(f"{self.name}.{key}={float(value):g} outside the SMW200A range "
                                  f"[{rng[0]:g}, {rng[1]:g}]")

    def _set_param(self, key, value):
        ch, _, param = key.partition("_")
        n = self.channels[ch]
        if param == "enable":
            self.t.write(f":OUTP{n}:STAT {'ON' if value else 'OFF'}")
        elif param == "frequency_hz":
            self.t.write(f":SOUR{n}:FREQ:CW {fmt(value)}")
        else:
            self.t.write(f":SOUR{n}:POW:LEV:IMM:AMPL {fmt(value)}")

    def get_state(self) -> dict:
        q = self.t.query
        state = {}
        for ch, n in self.channels.items():
            state[f"{ch}_enable"] = q(f":OUTP{n}:STAT?").strip() in ("1", "ON")
            state[f"{ch}_frequency_hz"] = float(q(f":SOUR{n}:FREQ:CW?"))
            state[f"{ch}_power_dbm"] = float(q(f":SOUR{n}:POW:LEV:IMM:AMPL?"))
            # read-only: anything but CW, unmodulated, no offsets, and the tone is not what it says
            state[f"{ch}_freq_mode"] = q(f":SOUR{n}:FREQ:MODE?").strip()
            state[f"{ch}_power_mode"] = q(f":SOUR{n}:POW:MODE?").strip()
            state[f"{ch}_modulation"] = q(f":SOUR{n}:MOD:STAT?").strip() in ("1", "ON")
            state[f"{ch}_iq"] = q(f":SOUR{n}:IQ:STAT?").strip() in ("1", "ON")
            state[f"{ch}_freq_offset_hz"] = float(q(f":SOUR{n}:FREQ:OFFS?"))
            state[f"{ch}_power_offset_db"] = float(q(f":SOUR{n}:POW:OFFS?"))
            state[f"{ch}_alc"] = q(f":SOUR{n}:POW:ALC:STAT?").strip()
        state["reference_source"] = q(":SOUR:ROSC:SOUR?").strip()     # INT / EXT
        self._local()
        return state

    def safe_off(self):
        for n in self.channels.values():
            self.t.write(f":OUTP{n}:STAT OFF")
        self._local()

    def _local(self):
        try:
            self.t.go_to_local()
        except TransportError:
            pass


class SimSMW200A(SimTransport):
    IDN = "Rohde&Schwarz,SMW200A,1412.0000K02/000000,5.20.043.47 (SIM)"

    def __init__(self, world=None):
        super().__init__(world)
        # the sim world's probe source; path 1 only, like the lab's unit
        self.s = {"on": False, "frequency_hz": 1e9, "power_dbm": -145.0}
        self.world.sources["rf_probe"] = self.s

    @scpi("OUTPut[:STATe]")
    def _out(self, q, a):
        if q:
            return "1" if self.s["on"] else "0"
        self.s["on"] = parse_bool(a)

    @scpi("[SOURce]:FREQuency[:CW]")
    def _freq(self, q, a):
        if q:
            return f"{self.s['frequency_hz']:.0f}"
        f = parse_number(a)
        if not FREQ_RANGE_HZ[0] <= f <= FREQ_RANGE_HZ[1]:
            raise ValueError(f"frequency {f} out of range")
        self.s["frequency_hz"] = round(f, 3)

    @scpi("[SOURce]:POWer[:LEVel][:IMMediate][:AMPLitude]")
    def _pow(self, q, a):
        if q:
            return f"{self.s['power_dbm']:g}"
        p = parse_number(a)
        if not POWER_RANGE_DBM[0] <= p <= POWER_RANGE_DBM[1]:
            raise ValueError(f"level {p} out of range")
        self.s["power_dbm"] = round(p, 2)

    @scpi("[SOURce]:FREQuency:MODE")
    def _fmode(self, q, a):
        return "CW"

    @scpi("[SOURce]:POWer:MODE")
    def _pmode(self, q, a):
        return "CW"

    @scpi("[SOURce]:MODulation[:ALL][:STATe]")
    def _mod(self, q, a):
        return "0"

    @scpi("[SOURce]:IQ:STATe")
    def _iq(self, q, a):
        return "0"

    @scpi("[SOURce]:FREQuency:OFFSet")
    def _foffs(self, q, a):
        return "0"

    @scpi("[SOURce]:POWer:OFFSet")
    def _poffs(self, q, a):
        return "0"

    @scpi("[SOURce]:POWer:ALC[:STATe]")
    def _alc(self, q, a):
        return "AUTO"

    @scpi("[SOURce]:ROSCillator:SOURce")
    def _rosc(self, q, a):
        return "INT"


SMW200A.SIM = SimSMW200A
