"""Mini-Circuits RCDAT-44G-63 programmable attenuator (Ethernet, HTTP).

0-63 dB in 0.25 dB steps. SETATT=<dB> and ATT? are the commands used in the notebooks
(note ATT? is fetched as http://ip/ATT? without the leading colon, as in
get_attenuator_state()).
"""
from __future__ import annotations

from ..transport import HttpTransport
from .base import Instrument, InstrumentError, fmt

STEP_DB = 0.25


class RCDAT(Instrument):
    kind = "attenuator"
    SETTERS = {"attenuation_db": "set_attenuation"}
    TIMEOUT_MS = 3000
    LIMITS = [("attenuation_db", "attenuation", "dB", 1)]

    @classmethod
    def open_transport(cls, cfg, rm):
        return HttpTransport(cfg["ip"], timeout_ms=cfg.get("timeout_ms", cls.TIMEOUT_MS))

    def idn(self) -> str:
        return f"Mini-Circuits RCDAT @ {self.cfg.get('ip')}"

    def errors(self, max_reads: int = 20) -> list[str]:
        return []

    def check_limit(self, key, value):
        super().check_limit(key, value)
        if key == "attenuation_db" and abs(value / STEP_DB - round(value / STEP_DB)) > 1e-9:
            raise InstrumentError(f"{self.name}: {value} dB is not a multiple of {STEP_DB} dB")

    def read_attenuation(self) -> float:
        return float(self.t.get("ATT?"))

    def set_attenuation(self, db):
        resp = self.t.query(f"SETATT={fmt(db)}")
        if resp.strip() == "0":     # 1 = ok on ours; 0 (failed) never seen, the read-back below checks anyway
            raise InstrumentError(f"{self.name}: SETATT={db} rejected")
        actual = self.read_attenuation()
        if abs(actual - db) > STEP_DB / 2:
            raise InstrumentError(f"{self.name}: requested {db} dB, reads back {actual} dB")

    def get_state(self) -> dict:
        return {"attenuation_db": self.read_attenuation()}


class SimRCDAT(HttpTransport):
    def __init__(self, world=None):
        from ..sim import SimWorld

        super().__init__("SIM")
        self.world = world or SimWorld()
        self.resource_name = "SIM::RCDAT"

    def get(self, path: str) -> str:
        cmd = path.lstrip(":").upper()
        if cmd == "ATT?":
            return f"{self.world.attenuation_db:g}"
        if cmd.startswith("SETATT="):
            try:
                db = float(cmd[7:])
            except ValueError:
                return "0"
            self.world.attenuation_db = min(63.0, max(0.0, round(db / STEP_DB) * STEP_DB))
            return "1" if 0 <= db <= 63 else "2"
        return "<html>" + " " * 120 + "</html>"


RCDAT.SIM = SimRCDAT
