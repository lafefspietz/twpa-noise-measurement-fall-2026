"""Windfreak SynthHD two-channel synthesizer (USB serial, via the `windfreak` package).

Channels get names in config.yaml (pump = B; channel A was the probe until the SMW200A
took over on 2026-10-19), and parameters are flattened to "<channel>_<param>", e.g.
pump_power_dbm. safe_off() turns off both channels, named or not.

Unlike the notebooks this never calls synth.init(): that forces the internal 27 MHz
reference and resets both channels to minimum frequency/power. Set
`reference_mode` in config if the reference has to be forced.
"""
from __future__ import annotations

from ..transport import TransportError
from .base import Instrument, InstrumentError

PARAMS = {"enable": bool, "frequency_hz": float, "power_dbm": float}
LIB_ATTR = {"enable": "enable", "frequency_hz": "frequency", "power_dbm": "power"}
DEFAULT_CHANNELS = {"pump": 1}
N_CHANNELS = 2


class WindfreakSynthHD(Instrument):
    kind = "rf_source"
    TUNABLES = []       # USB serial: the windfreak package has no timeout to set

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

    @classmethod
    def open_transport(cls, cfg, rm):
        import serial
        import serial.tools.list_ports
        from windfreak import SynthHD

        port = cfg.get("port")
        if cfg.get("hwid_match"):
            hits = [p.device for p in serial.tools.list_ports.comports() if cfg["hwid_match"] in p.hwid]
            if len(hits) == 1:
                port = hits[0]
            elif len(hits) > 1:
                raise TransportError(f"several COM ports match {cfg['hwid_match']!r}: {hits}")
        if not port:
            raise TransportError("Windfreak COM port not found (set port or hwid_match)")
        try:
            return SynthHD(port)
        except (serial.SerialException, OSError) as e:
            raise TransportError(f"{port}: {e}") from e

    def _io(self, fn):
        try:
            return fn()
        except (ValueError, TypeError) as e:
            raise InstrumentError(f"{self.name}: {e}") from e
        except Exception as e:   # serial errors, garbled replies
            raise TransportError(f"{self.name}: {type(e).__name__}: {e}") from e

    def initialize(self):
        if self.cfg.get("reference_mode"):
            self._io(lambda: setattr(self.t, "reference_mode", self.cfg["reference_mode"]))

    def idn(self) -> str:
        return self._io(lambda: f"Windfreak {self.t.model}, sn {self.t.serial_number}, "
                                f"fw {self.t.firmware_version}")

    def errors(self, max_reads: int = 20) -> list[str]:
        return []

    def settable(self) -> list[str]:
        return [f"{ch}_{p}" for ch in self.channels for p in PARAMS]

    def _set_param(self, key, value):
        ch, _, param = key.partition("_")
        channel = self.t[self.channels[ch]]
        self._io(lambda: setattr(channel, LIB_ATTR[param], PARAMS[param](value)))

    def get_state(self) -> dict:
        state = {}
        for ch, idx in self.channels.items():
            c = self.t[idx]
            state[f"{ch}_enable"] = self._io(lambda: bool(c.enable))
            state[f"{ch}_frequency_hz"] = self._io(lambda: float(c.frequency))
            state[f"{ch}_power_dbm"] = self._io(lambda: float(c.power))
            state[f"{ch}_locked"] = self._io(lambda: bool(c.lock_status))
        state["reference_mode"] = self._io(lambda: self.t.reference_mode)
        return state

    def safe_off(self):
        for idx in range(N_CHANNELS):
            c = self.t[idx]
            self._io(lambda: setattr(c, "enable", False))


class _FakeChannel:
    """Mimics windfreak.SynthHDChannel; mirrors its output into SimWorld.sources."""

    def __init__(self, world, source_name):
        self._s = {"on": False, "frequency_hz": 53e6, "power_dbm": -80.0}
        world.sources[source_name] = self._s

    @property
    def enable(self):
        return self._s["on"]

    @enable.setter
    def enable(self, v):
        self._s["on"] = bool(v)

    @property
    def frequency(self):
        return self._s["frequency_hz"]

    @frequency.setter
    def frequency(self, hz):
        if not 10e6 <= hz <= 15e9:
            raise ValueError(f"Expected float in range [10e6, 15e9], got {hz}")
        self._s["frequency_hz"] = float(hz)

    @property
    def power(self):
        return self._s["power_dbm"]

    @power.setter
    def power(self, dbm):
        if not isinstance(dbm, (float, int)):
            raise TypeError("Expected float or int.")
        if not -80 <= dbm <= 20:
            raise ValueError(f"Expected float in range [-80, 20], got {dbm}")
        self._s["power_dbm"] = round(float(dbm), 2)

    @property
    def lock_status(self):
        return True


class FakeSynthHD:
    model = "SynthHD v2 (SIM)"
    serial_number = 0
    firmware_version = "SIM"
    resource_name = "SIM::SynthHD"

    def __init__(self, world=None):
        from ..sim import SimWorld

        self.world = world or SimWorld()
        self.reference_mode = "external"
        # channel A is not cabled to anything in the sim (the probe is the SMW200A)
        self._channels = [_FakeChannel(self.world, "rf_synth_a"), _FakeChannel(self.world, "rf_pump")]

    def __getitem__(self, i):
        return self._channels[i]

    def close(self):
        pass


WindfreakSynthHD.SIM = FakeSynthHD
