"""Agilent 33220A function generator, used as a DC source (hot-wire noise source bias).

Commands from twpa_calibration.ipynb and noise-sweep-zero-power.ipynb:
FUNC DC, VOLT:OFFS <V>, OUTP ON/OFF.
"""
from __future__ import annotations

from ..sim import SimTransport, scpi
from ..sim.scpi import parse_bool, parse_number
from .base import Instrument, fmt


class Fgen33220A(Instrument):
    kind = "dc_source"
    SETTERS = {
        "function": "set_function",
        "offset_v": "set_offset",
        "output": "set_output",
    }
    LIMITS = [("offset_v", "DC offset", "V", 1)]

    def apply(self, **params) -> dict:
        rank = {"function": 1, "offset_v": 2, "output": 3 if params.get("output") else 0}
        return super().apply(**dict(sorted(params.items(), key=lambda kv: rank.get(kv[0], 5))))

    def set_function(self, func):
        self.t.write(f"FUNC {func}")

    def set_offset(self, volts):
        self.t.write(f"VOLT:OFFS {fmt(volts)}")

    def set_output(self, on):
        self.t.write(f"OUTP {'ON' if on else 'OFF'}")

    def get_state(self) -> dict:
        return {
            "function": self.t.query("FUNC?").strip(),
            "offset_v": float(self.t.query("VOLT:OFFS?")),
            "output": self.t.query("OUTP?").strip() in ("1", "ON"),
            "load": self.t.query("OUTP:LOAD?").strip(),           # 50 or 9.9E+37 (high Z)
        }

    def safe_off(self):
        self.set_output(False)


class Sim33220A(SimTransport):
    IDN = "Agilent Technologies,33220A,SIM00000,2.07-2.06-22-2"
    FUNCS = ("SIN", "SQU", "RAMP", "PULS", "NOIS", "DC", "USER")

    def __init__(self, world=None):
        super().__init__(world)
        self.s = dict(func="SIN", offs=0.0, out=False, load=50.0)

    def _publish(self):
        self.world.noise_source_v = self.s["offs"] if self.s["out"] and self.s["func"] == "DC" else 0.0

    @scpi("[SOURce]:FUNCtion[:SHAPe]")
    def _func(self, q, a):
        if q:
            return self.s["func"]
        f = a.upper()[:4].rstrip()
        f = {"SQUA": "SQU", "PULS": "PULS", "NOIS": "NOIS", "SINU": "SIN"}.get(f, f)
        if f not in self.FUNCS:
            raise ValueError(a)
        self.s["func"] = f
        self._publish()

    @scpi("[SOURce]:VOLTage:OFFSet")
    def _offs(self, q, a):
        if q:
            return f"{self.s['offs']:+.8E}"
        v = parse_number(a)
        if abs(v) > (5.0 if self.s["load"] == 50.0 else 10.0):
            raise ValueError(f"offset {v} V out of range")
        self.s["offs"] = v
        self._publish()

    @scpi("OUTPut[:STATe]")
    def _out(self, q, a):
        if q:
            return "1" if self.s["out"] else "0"
        self.s["out"] = parse_bool(a)
        self._publish()

    @scpi("OUTPut:LOAD")
    def _load(self, q, a):
        if q:
            return f"{self.s['load']:+.8E}"
        self.s["load"] = 9.9e37 if a.upper().startswith("INF") else parse_number(a)


Fgen33220A.SIM = Sim33220A
