"""Rohde & Schwarz ZVA24 vector network analyzer (via RsInstrument).

The trace read is the sequence from vna-trace.ipynb / qubit-flux-sweep.ipynb:
FORM REAL,32; CALC1:PAR:SEL 'Trc1'; INIT:CONT OFF; INIT:IMM; *OPC?;
CALC1:DATA? SDAT; INIT:CONT ON; go_to_local(). The notebooks hand the front panel back
after every trace; here that happens once, when reading stops (idle(), close()), because
switching remote/local for every live frame made the screen flash.
"""
from __future__ import annotations

import numpy as np

from ..sim import SimBlock, SimTransport, scpi
from ..sim.scpi import parse_bool, parse_number
from ..transport import RsTransport, TransportError, find_resource
from .base import Instrument, MeasurementError, Trace, fmt


def s_db(s):
    return 20 * np.log10(np.abs(s))


class ZVA(Instrument):
    kind = "vna"
    SETTERS = {
        "start_hz": "set_start",
        "stop_hz": "set_stop",
        "points": "set_points",
        "ifbw_hz": "set_ifbw",
        "power_dbm": "set_power",
        "output": "set_output",
        "sweep_type": "set_sweep_type",
        "cw_hz": "set_cw",
    }
    TIMEOUT_MS = 10000
    LIMITS = [("start_hz", "start", "GHz", 1e9), ("stop_hz", "stop", "GHz", 1e9), ("cw_hz", "CW", "GHz", 1e9),
              ("points", "points", "", 1), ("ifbw_hz", "IFBW", "Hz", 1), ("power_dbm", "power", "dBm", 1)]
    TUNABLES = Instrument.TUNABLES + [
        {"key": "max_retries", "label": "trace read attempts", "kind": "int", "default": 5, "min": 1,
         "help": "Per trace in experiments (the live view has its own count)."},
        {"key": "retry_delay_s", "label": "wait between attempts", "unit": "s", "default": 0.1, "min": 0},
    ]

    def __init__(self, name, transport, cfg=None):
        super().__init__(name, transport, cfg)
        self.trace_name = self.cfg.get("trace_name", "Trc1")
        # Put the VNA back in continuous mode + local when reading stops, so the front
        # panel keeps working (the notebooks did this after every trace).
        self.restore_continuous = self.cfg.get("restore_continuous", True)
        self._single = False   # INIT:CONT OFF sent and not undone yet
        self._axis = None

    def reconfigure(self):
        super().reconfigure()
        self.max_retries = self.cfg.get("max_retries", 5)
        self.retry_delay_s = self.cfg.get("retry_delay_s", 0.1)

    def initialize(self):
        super().initialize()
        # Keep drawing traces while in remote; the ZVA default (OFF) shows a blank remote screen.
        self.t.write("SYSTem:DISPlay:UPDate ON")

    @classmethod
    def open_transport(cls, cfg, rm):
        resource = find_resource(rm.list_resources(), cfg["match"])
        return RsTransport.open(
            resource, timeout_ms=cfg.get("timeout_ms", cls.TIMEOUT_MS),
            options=cfg.get("options", "DriverSetup=(VxiCapable=False, ViClearExeMode=IgnoreError)"))

    def apply(self, **params) -> dict:
        # The ZVA refuses FREQ:STARt/STOP in CW mode (-200 "Function not available"), so a
        # sweep type change goes first when it leaves CW (start/stop then land in LIN) and
        # last when it enters CW (start/stop are set while still in LIN).
        if "sweep_type" in params:
            kind = params.pop("sweep_type")
            if str(kind).upper().startswith("CW"):
                params = {**params, "sweep_type": kind}
            else:
                params = {"sweep_type": kind, **params}
        return super().apply(**params)

    # ---- setters ----
    def set_start(self, hz):
        self.t.write(f":SENS1:FREQ:START {fmt(hz)}")
        self._axis = None

    def set_stop(self, hz):
        self.t.write(f":SENS1:FREQ:STOP {fmt(hz)}")
        self._axis = None

    def set_points(self, n):
        self.t.write(f":SENS1:SWEep:POINts {int(n)}")
        self._axis = None

    def set_ifbw(self, hz):
        self.t.write(f":SENS1:BAND {fmt(hz)}")
        self._axis = None

    def set_power(self, dbm):
        self.t.write(f"SOURce1:POWer {fmt(dbm)}")
        self._axis = None           # power_dbm goes into the trace meta

    def set_output(self, on):
        self.t.write(f"OUTPut:STATe {'ON' if on else 'OFF'}")

    def set_sweep_type(self, kind):
        """LIN (frequency sweep) or CW (fixed frequency, x axis = time)."""
        self.t.write(f"SENSe1:SWEep:TYPE {kind}")
        self._axis = None

    def set_cw(self, hz):
        self.t.write(f"SENSe1:FREQuency:CW {fmt(hz)} HZ")
        self._axis = None

    # ---- state ----
    def get_state(self) -> dict:
        q = lambda cmd: self.t.query(cmd).strip()
        sweep_type = q("SENSe1:SWEep:TYPE?")
        # In CW mode the ZVA doesn't answer FREQ:STARt?/STOP? (no reply, then -200
        # "Function not available" in the error queue), so they are None there.
        cw = sweep_type.upper().startswith("CW")
        state = {
            "start_hz": None if cw else float(q(":SENS1:FREQ:START?")),
            "stop_hz": None if cw else float(q(":SENS1:FREQ:STOP?")),
            "points": int(q(":SENS1:SWEep:POINts?")),
            "ifbw_hz": float(q(":SENS1:BAND?")),
            "power_dbm": float(q("SOURce1:POWer?")),
            "output": int(q("OUTPut:STATe?")) == 1,
            "sweep_type": sweep_type,
            "cw_hz": float(q("SENSe1:FREQuency:CW?")),
            "sweep_time_s": float(q("SENSe1:SWEep:TIME?")),
            # sweeps per single-sweep trigger (INIT:IMM runs all of them before *OPC?)
            "sweep_count": int(float(self.query_optional("SENSe1:SWEep:COUNt?", "1"))),
        }
        self._axis = state
        return self.remember_state(state)

    def _get_axis(self) -> dict:
        if self._axis is None:
            self.get_state()
        return self._axis

    def forget_cached_state(self):
        self._axis = None

    def expected_sweep_s(self) -> float:
        """How long one read_trace() keeps the instrument busy."""
        ax = self._get_axis()
        return ax["sweep_time_s"] * max(1, ax["sweep_count"])

    # ---- measurements ----
    def _single_sweep(self) -> np.ndarray:
        self.t.write("FORM REAL,32")
        self.t.write(f":CALC1:PAR:SEL '{self.trace_name}'")
        if not self._single:
            self.t.write("INITiate:CONTinuous OFF")
            self._single = True
        self.t.write("INITiate:IMMediate")
        opc = self.t.query("*OPC?").strip()
        if opc != "1":
            raise MeasurementError(f"*OPC? returned {opc!r}")
        raw = self.t.query_binary_values(":CALC1:DATA? SDAT")
        if len(raw) % 2 or np.isnan(raw).any():
            raise MeasurementError(f"bad trace (len={len(raw)})")
        return raw[0::2] + 1j * raw[1::2]

    def read_trace(self, max_retries: int | None = None) -> Trace:
        """One sweep; y is complex S (real + 1j*imag), like the notebooks
        (max_retries overrides the configured count, e.g. for the live view)."""
        ax = self._get_axis()
        with self.t.timeout_at_least(self.expected_sweep_s() * 2000 + 2000):
            s = self.retry(self._single_sweep, what="trace read",
                           max_retries=max_retries or self.max_retries, delay_s=self.retry_delay_s)
        if len(s) != ax["points"]:
            self._axis = None       # settings changed behind our back (front panel): re-read next time
            raise MeasurementError(f"{self.name}: got {len(s)} points, expected {ax['points']}")
        if ax["sweep_type"].upper().startswith("CW"):
            x, x_unit = np.linspace(0, ax["sweep_time_s"], ax["points"]), "s"
        else:
            x, x_unit = np.linspace(ax["start_hz"], ax["stop_hz"], ax["points"]), "Hz"
        return Trace(x=x, y=s, x_unit=x_unit, y_unit="S21",
                     meta={k: ax[k] for k in ("ifbw_hz", "power_dbm", "sweep_type", "cw_hz")})

    def idle(self):
        """Reading stopped (live view closed or paused): continuous sweep + local."""
        if self.restore_continuous and self._single:
            self.t.write("INITiate:CONTinuous ON")
            self.t.go_to_local()
            self._single = False

    def close(self):
        try:
            self.idle()
        except TransportError:
            pass
        super().close()

    def safe_off(self):
        self.set_output(False)


class SimZVA(SimTransport):
    IDN = "Rohde-Schwarz,ZVA24-2Port,SIM0000000,3.30"

    def __init__(self, world=None):
        super().__init__(world)
        self.s = dict(start=5.525e9, stop=5.625e9, points=401, ifbw=1e3, power=-30.0, out=False,
                      type="LIN", cw=5.58e9, cont=True, form="ASC", par="Trc1", count=1)
        self._last = None
        self._publish()

    def sweep_time(self):
        return self.s["points"] / self.s["ifbw"] * 1.2 + 0.005

    def _publish(self):
        s = self.s
        f = s["cw"] if s["type"] == "CW" else (s["start"] + s["stop"]) / 2
        self.world.sources["vna"] = {"on": s["out"], "frequency_hz": f, "power_dbm": s["power"]}

    def _num(self, key, q, a, cast=float):
        if q:
            return f"{self.s[key]:.10g}"
        self.s[key] = cast(parse_number(a))
        self._publish()

    def _span_in_cw(self, q, cmd) -> bool:
        """The real ZVA refuses FREQ:STARt/STOP in CW mode with -200: a query gets no reply,
        a write is ignored. True when the write was refused."""
        if self.s["type"] != "CW":
            return False
        self.push_error(-200, f"Execution error;Function not available;{cmd}")
        if q:
            raise TransportError(f"{cmd}: timeout (sim: function not available in CW mode)")
        return True

    @scpi("[SENSe]:FREQuency:STARt")
    def _start(self, q, a):
        if self._span_in_cw(q, ":SENS1:FREQ:START" + ("?" if q else f" {a}")):
            return None
        return self._num("start", q, a)

    @scpi("[SENSe]:FREQuency:STOP")
    def _stop(self, q, a):
        if self._span_in_cw(q, ":SENS1:FREQ:STOP" + ("?" if q else f" {a}")):
            return None
        return self._num("stop", q, a)

    @scpi("[SENSe]:FREQuency:CW")
    def _cw(self, q, a): return self._num("cw", q, a)

    @scpi("[SENSe]:SWEep:POINts")
    def _points(self, q, a): return self._num("points", q, a, cast=int)

    @scpi("[SENSe]:BANDwidth[:RESolution]")
    def _ifbw(self, q, a): return self._num("ifbw", q, a)

    @scpi("[SENSe]:SWEep:TIME")
    def _swt(self, q, a):
        if q:
            return f"{self.sweep_time():.10g}"

    @scpi("[SENSe]:SWEep:COUNt")
    def _count(self, q, a): return self._num("count", q, a, cast=int)

    @scpi("[SENSe]:SWEep:TYPE")
    def _type(self, q, a):
        if q:
            return self.s["type"]
        t = a.upper()[:3]
        if t not in ("LIN", "LOG", "CW", "POW", "SEG"):
            raise ValueError(a)
        self.s["type"] = t
        self._publish()

    @scpi("SOURce:POWer[:LEVel][:IMMediate][:AMPLitude]")
    def _power(self, q, a): return self._num("power", q, a)

    @scpi("OUTPut[:STATe]")
    def _out(self, q, a):
        if q:
            return "1" if self.s["out"] else "0"
        self.s["out"] = parse_bool(a)
        self._publish()

    @scpi("FORMat[:DATA]")
    def _form(self, q, a):
        f = a.replace(" ", "").upper()
        self.s["form"] = "REAL" if f == "REAL,32" else "ASC"

    @scpi("CALCulate:PARameter:SELect")
    def _par(self, q, a):
        self.s["par"] = a.strip("'\"")

    @scpi("SYSTem:DISPlay:UPDate")
    def _disp_update(self, q, a):
        if q:
            return "1" if self.s.get("disp_update") else "0"
        self.s["disp_update"] = parse_bool(a)

    @scpi("INITiate:CONTinuous")
    def _cont(self, q, a):
        if q:
            return "1" if self.s["cont"] else "0"
        self.s["cont"] = parse_bool(a)

    @scpi("INITiate[:IMMediate]")
    def _init(self, q, a):
        self.start_operation(self.sweep_time() * self.s["count"])
        self._last = self._generate()

    @scpi("CALCulate:DATA")
    def _data(self, q, a):
        if self.world.rng.random() < self.world.fail_rate:
            raise TransportError("CALC:DATA?: timeout (sim: injected failure)")
        s = self._last if self._last is not None else self._generate()
        raw = np.empty(2 * len(s))
        raw[0::2], raw[1::2] = s.real, s.imag
        return SimBlock(raw, binary=self.s["form"] == "REAL")

    def _generate(self):
        s = self.s
        if s["type"] == "CW":
            freqs = np.full(s["points"], s["cw"])
        else:
            freqs = np.linspace(s["start"], s["stop"], s["points"])
        return self.world.vna_s21(freqs, s["ifbw"])


ZVA.SIM = SimZVA
