"""Yokogawa GS210 DC voltage/current source, used as the qubit flux bias.

Commands from the flux-sweep notebooks (:SOUR:LEV, :OUTPut:STATe?) and
twpa_calibration.ipynb (:SOUR:FUNC, :SOUR:RANG, :OUTP). The same unit was also used
as the noise-diode bias; to do that, change `limits` in config.yaml after recabling.

Safety: turning the output on re-checks the level already in the instrument against
`limits`, so a level left from another use (or the front panel) is never applied.
The level means volts or amps depending on the source function, so the mode is
checked too: limits.level is for VOLT, limits.level_a for CURR. Current mode is
refused altogether (switching to it, setting a level in it, turning the output on in
it) unless `allow_current_mode` is true, e.g. if the front panel left it in CURR.
Ramping (`ramp_step`, `ramp_delay_s`): level changes while the output is on are done
in steps, so the bias never jumps by more than ramp_step.
"""
from __future__ import annotations

import time

import numpy as np

from ..sim import SimTransport, scpi
from ..sim.scpi import parse_bool, parse_number
from .base import Instrument, InstrumentError, fmt

VOLT_RANGES = (10e-3, 100e-3, 1.0, 10.0, 30.0)
CURR_RANGES = (1e-3, 10e-3, 100e-3, 200e-3)


class GS210(Instrument):
    kind = "dc_source"
    SETTERS = {
        "function": "set_function",
        "range": "set_range",
        "level": "set_level",
        "output": "set_output",
    }
    LIMITS = [("level", "level (VOLT mode)", "V", 1), ("level_a", "level (CURR mode)", "mA", 1e-3)]
    SAFETY = [{"key": "allow_current_mode", "label": "allow current mode (CURR)", "kind": "bool", "default": False,
               "help": "Off: CURR is refused, and so are level changes and output on while the source is in "
                       "CURR (e.g. from the front panel). On: the CURR-mode limit applies."}]
    TUNABLES = Instrument.TUNABLES + [
        {"key": "ramp_step", "label": "ramp step", "unit": "V", "default": None, "min": 0, "nullable": True,
         "help": "With the output on, level changes go in steps of at most this. Empty = no ramp."},
        {"key": "ramp_delay_s", "label": "wait per ramp step", "unit": "s", "default": 0.05, "min": 0},
    ]

    def reconfigure(self):
        super().reconfigure()
        self.ramp_step = self.cfg.get("ramp_step")
        self.ramp_delay_s = self.cfg.get("ramp_delay_s", 0.05)
        self.allow_current_mode = bool(self.cfg.get("allow_current_mode", False))

    def function(self) -> str:
        """VOLT or CURR: the one this apply() sets, else the instrument's."""
        f = self._pending.get("function") or self.t.query(":SOUR:FUNC?")
        return "CURR" if str(f).strip().upper().startswith("CURR") else "VOLT"

    def check_limit(self, key, value):
        if key == "function" and str(value).strip().upper().startswith("CURR") and not self.allow_current_mode:
            raise InstrumentError(f"{self.name}: current mode is off in the settings (allow current mode)")
        if key == "level" and value is not None and self.function() == "CURR":
            if not self.allow_current_mode:
                raise InstrumentError(f"{self.name} is in current mode (CURR), which is off in the settings: "
                                      f"set the function to VOLT first")
            self.check_range("level_a", float(value), f"level={float(value):g} A (CURR mode)")
            return
        super().check_limit(key, value)

    def apply(self, **params) -> dict:
        # Turning off goes first, turning on goes last, and function/range are set
        # before the level that depends on them.
        rank = {"function": 1, "range": 2, "level": 3, "output": 4 if params.get("output") else 0}
        return super().apply(**dict(sorted(params.items(), key=lambda kv: rank.get(kv[0], 5))))

    def set_function(self, func):
        func = func.upper()
        if func not in ("VOLT", "VOLTAGE", "CURR", "CURRENT"):
            raise InstrumentError(f"{self.name}: function must be VOLT or CURR, not {func!r}")
        self.t.write(f":SOUR:FUNC {func}")

    def set_range(self, rng):
        self.t.write(f":SOUR:RANG {fmt(rng)}")

    def set_level(self, level):
        level = float(level)
        if self.ramp_step and self.output_on():
            start = self.level()
            n = int(np.ceil(abs(level - start) / self.ramp_step))
            for v in np.linspace(start, level, n + 1)[1:-1]:
                self.t.write(f":SOUR:LEV {fmt(v)}")
                time.sleep(self.ramp_delay_s)
        self.t.write(f":SOUR:LEV {fmt(level)}")

    def set_output(self, on):
        if on:
            self.check_limit("level", self.level())
        self.t.write(f":OUTP {'ON' if on else 'OFF'}")

    def output_on(self) -> bool:
        return self.t.query(":OUTP?").strip() in ("1", "ON")

    def level(self) -> float:
        return float(self.t.query(":SOUR:LEV?"))

    def get_state(self) -> dict:
        return {
            "function": self.t.query(":SOUR:FUNC?").strip(),
            "range": float(self.t.query(":SOUR:RANG?")),
            "level": self.level(),
            "output": self.output_on(),
        }

    def safe_off(self):
        self.set_output(False)


class SimGS210(SimTransport):
    IDN = "YOKOGAWA,GS210,SIM00000,2.02"

    def __init__(self, world=None):
        super().__init__(world)
        self.s = dict(func="VOLT", rng=10.0, lev=0.0, out=False)

    def _publish(self):
        self.world.yoko_v = self.s["lev"] if self.s["out"] and self.s["func"] == "VOLT" else 0.0

    @scpi("[SOURce]:FUNCtion")
    def _func(self, q, a):
        if q:
            return self.s["func"]
        f = a.upper()[:4]
        if f not in ("VOLT", "CURR"):
            raise ValueError(a)
        self.s["func"] = f
        self.s["lev"] = 0.0
        self._publish()

    @scpi("[SOURce]:RANGe")
    def _rng(self, q, a):
        if q:
            return f"{self.s['rng']:.6E}"
        ranges = VOLT_RANGES if self.s["func"] == "VOLT" else CURR_RANGES
        v = abs(parse_number(a))
        fit = [r for r in ranges if r >= v * (1 - 1e-9)]
        if not fit:
            raise ValueError(a)
        self.s["rng"] = fit[0]
        if abs(self.s["lev"]) > self.s["rng"] * 1.2:
            self.s["lev"] = 0.0
        self._publish()

    @scpi("[SOURce]:LEVel")
    def _lev(self, q, a):
        if q:
            return f"{self.s['lev']:+.6E}"
        v = parse_number(a)
        if abs(v) > self.s["rng"] * 1.2:      # GS200 allows 120 % of range
            raise ValueError(f"{v} outside range {self.s['rng']}")
        self.s["lev"] = v
        self._publish()

    @scpi("OUTPut[:STATe]")
    def _out(self, q, a):
        if q:
            return "1" if self.s["out"] else "0"
        self.s["out"] = parse_bool(a)
        self._publish()


GS210.SIM = SimGS210
