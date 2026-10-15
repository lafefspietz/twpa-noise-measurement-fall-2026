"""Mini-Circuits RC-4SPDT-A18 warm RF switch box (Ethernet, HTTP).

Each physical channel (A-D) gets a name and named positions in config.yaml, so
callers write apply(receiver="spa") instead of remembering channel/port numbers.
Commands SWPORT? and SETP=<byte> are the ones used in twpa_calibration.ipynb.
"""
from __future__ import annotations

from ..transport import HttpTransport
from .base import Instrument, InstrumentError

CHANNELS = "ABCD"


def decode_ports(raw: str) -> dict[str, int]:
    """SWPORT? reply -> {'A': 1|2, ...}. Bit n set = channel n on port 2.
    The device reports 128 + bits; only the low four bits matter."""
    bits = int(raw.strip()) & 0x0F
    return {ch: ((bits >> i) & 1) + 1 for i, ch in enumerate(CHANNELS)}


def encode_ports(ports: dict[str, int]) -> int:
    return 128 + sum((ports[ch] - 1) << i for i, ch in enumerate(CHANNELS))


class RC4SPDT(Instrument):
    kind = "rf_switch"
    TIMEOUT_MS = 3000

    def __init__(self, name, transport, cfg=None):
        super().__init__(name, transport, cfg)
        # name -> {"channel": "B", "positions": {"vna": 1, "spa": 2}}
        self.switches = self.cfg.get("switches", {})
        for sw, spec in self.switches.items():
            if spec["channel"] not in CHANNELS or set(spec["positions"].values()) - {1, 2}:
                raise InstrumentError(f"{name}.{sw}: bad config {spec}")

    @classmethod
    def open_transport(cls, cfg, rm):
        return HttpTransport(cfg["ip"], timeout_ms=cfg.get("timeout_ms", cls.TIMEOUT_MS))

    def idn(self) -> str:
        return f"Mini-Circuits RC-4SPDT @ {self.cfg.get('ip')}"   # no *IDN? over HTTP

    def errors(self, max_reads: int = 20) -> list[str]:
        return []   # no error queue; failures surface as TransportError

    def read_ports(self) -> dict[str, int]:
        return decode_ports(self.t.query("SWPORT?"))

    def get_state(self) -> dict:
        ports = self.read_ports()
        state = {}
        for sw, spec in self.switches.items():
            port = ports[spec["channel"]]
            labels = [k for k, v in spec["positions"].items() if v == port]
            state[sw] = labels[0] if labels else port
        state["ports"] = ports
        return state

    def apply(self, **params) -> dict:
        """apply(receiver="spa", rx_source="fridge"). Values are position names
        (or 1/2). All requested switches move in one SETP write, then read back."""
        unknown = set(params) - set(self.switches)
        if unknown:
            raise InstrumentError(f"{self.name}: unknown switch(es) {sorted(unknown)}; "
                                  f"known: {sorted(self.switches)}")
        ports = self.read_ports()
        for sw, value in params.items():
            spec = self.switches[sw]
            if value in (1, 2):
                port = value
            elif value in spec["positions"]:
                port = spec["positions"][value]
            else:
                raise InstrumentError(f"{self.name}.{sw}: unknown position {value!r}; "
                                      f"known: {sorted(spec['positions'])}")
            ports[spec["channel"]] = port
        self.t.write(f"SETP={encode_ports(ports)}")
        actual = self.read_ports()
        if actual != ports:
            raise InstrumentError(f"{self.name}: requested {ports}, switch reports {actual}")
        return self.get_state()


class SimRC4SPDT(HttpTransport):
    def __init__(self, world=None):
        from ..sim import SimWorld

        super().__init__("SIM")
        self.world = world or SimWorld()
        self.resource_name = "SIM::RC4SPDT"

    def get(self, path: str) -> str:
        cmd = path.lstrip(":").upper()
        if cmd == "SWPORT?":
            return str(encode_ports(self.world.switch_ports))
        if cmd.startswith("SETP="):
            try:
                bits = int(cmd[5:]) & 0x0F
            except ValueError:
                return "0"
            self.world.switch_ports.update(
                {ch: ((bits >> i) & 1) + 1 for i, ch in enumerate(CHANNELS)})
            return "1"
        return "<html>" + " " * 120 + "</html>"   # what the device sends for unknown commands


RC4SPDT.SIM = SimRC4SPDT
