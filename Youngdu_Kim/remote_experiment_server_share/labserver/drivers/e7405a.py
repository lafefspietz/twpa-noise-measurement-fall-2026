"""HP/Agilent E7405A (ESA-E series) spectrum analyzer.

Commands come from the notebooks (twpa_calibration, noise-sweep-zero-power,
noise-diode-y-factor) or were checked on our instrument.
"""
from __future__ import annotations

import logging
import time

import numpy as np

from ..sim import SimBlock, SimTransport, scpi
from ..sim.scpi import parse_bool, parse_number
from ..transport import TransportError
from .base import Instrument, InstrumentError, MeasurementError, Trace, fmt

log = logging.getLogger("labserver.drivers")

N_POINTS = 401   # ESA-E traces are fixed at 401 points

# STATus:QUEStionable:CALibration condition bits (E7400A Programmer's Guide E7401-90053, p. 84-85)
CAL_FAILURE_BITS = {2: "TG", 3: "RF", 4: "IF", 5: "LO", 6: "ADC", 7: "FM demod", 8: "misc/sys"}
CAL_RF_SKIPPED, CAL_RF_NEEDED, CAL_NEEDED = 1 << 11, 1 << 12, 1 << 14
OPER_CALIBRATING = 1        # STATus:OPERation condition bit 0
CAL_NEEDS_CABLE = "+652"    # "Connect Calibration Output to Input": CAL:ALL without the cable, RF skipped
# A command garbled on the USB-GPIB link comes back as SCPI command errors (-102 Syntax
# error, -113 Undefined header) or -420 Query UNTERMINATED, in bursts between read
# timeouts (seen 2026-10-19): an apply() that gets only those sends its settings again.
APPLY_ATTEMPTS = 3


def garbled(errors: list[str]) -> bool:
    """Only the errors a garbled transfer gives (not a value the SA refused, -2xx)."""
    return bool(errors) and all(e.startswith(("-1", "-420")) for e in errors)


def decode_alignment(cond: int) -> dict:
    """The QUEStionable:CALibration condition register as flags (cond < 0: not read)."""
    if cond < 0:
        return {"cal_condition": cond, "known": False}
    failed = [name for bit, name in CAL_FAILURE_BITS.items() if cond & (1 << bit)]
    return {"cal_condition": cond, "known": True,
            "align_needed": bool(cond & CAL_NEEDED),            # e.g. a temperature change with auto align off
            "align_rf_needed": bool(cond & CAL_RF_NEEDED),
            "align_rf_skipped": bool(cond & CAL_RF_SKIPPED),
            # without the cable the RF part is skipped and flagged failed too: not a fault of the rest
            "failed": [f for f in failed if not (f == "RF" and cond & CAL_RF_SKIPPED)]}


def dbm_to_w(dbm):
    return 10 ** (np.asarray(dbm) / 10) * 1e-3


# dBm = dB<unit> + offset, for 50 ohm: 0 dBmV = 1 mV = -46.99 dBm, 0 dBuV = -106.99 dBm
_DB_OFFSETS = {"DBM": 0.0, "DBMV": -46.9897, "DBUV": -106.9897}


def level_to_dbm(value: float, unit: str) -> float:
    """A level in an SA unit (DBM, DBMV, DBUV, W, V into 50 ohm) in dBm."""
    u = str(unit).strip().upper()
    if u in _DB_OFFSETS:
        return value + _DB_OFFSETS[u]
    if u in ("W", "V"):
        if value <= 0:
            return -np.inf
        return 10 * np.log10(value * 1e3) if u == "W" else 20 * np.log10(value) + 13.0103
    raise InstrumentError(f"unknown SA unit {unit!r}")


class E7405A(Instrument):
    kind = "spectrum_analyzer"
    SETTERS = {
        "center_hz": "set_center",
        "span_hz": "set_span",
        "rbw_hz": "set_rbw",
        "vbw_hz": "set_vbw",
        "detector": "set_detector",
        "ref_level": "set_ref_level",
        "unit": "set_unit",
        "sweep_time_s": "set_sweep_time",
        "continuous": "set_continuous",
        "attenuation_db": "set_attenuation",
        "average_type": "set_average_type",
        "scale": "set_scale",
    }
    SCALES = ("LOG", "LIN")
    LIMITS = [("center_hz", "center", "GHz", 1e9), ("span_hz", "span", "MHz", 1e6),
              ("rbw_hz", "RBW", "kHz", 1e3), ("vbw_hz", "VBW", "kHz", 1e3),
              ("ref_level_dbm", "ref level", "dBm", 1), ("sweep_time_s", "sweep time", "s", 1),
              ("attenuation_db", "input attenuation", "dB", 1)]
    TUNABLES = Instrument.TUNABLES + [
        {"key": "max_retries", "label": "trace read attempts", "kind": "int", "default": 50, "min": 1,
         "help": "Per trace in experiments (the live view has its own count). The notebooks retried 50 times."},
        {"key": "retry_delay_s", "label": "wait between attempts", "unit": "s", "default": 0.1, "min": 0},
        {"key": "settle_after_read_s", "label": "wait after each zero-span read", "unit": "s", "default": 0.05,
         "min": 0, "help": "The notebooks waited 50 ms."},
    ]

    def __init__(self, name, transport, cfg=None):
        super().__init__(name, transport, cfg)
        # Single-sweep mode: every read_trace() starts a fresh sweep after any setting
        # change, which is what parameter sweeps need. close() hands the front panel
        # back in continuous mode.
        self.single_sweep = self.cfg.get("single_sweep", True)
        self._continuous = None   # last known INIT:CONT state
        self._axis = None   # cached sweep geometry, see _get_axis()

    def initialize(self):
        super().initialize()
        self.t.write(":FORM ASC")      # ASCII traces, as in the notebooks

    def reconfigure(self):
        super().reconfigure()
        self.max_retries = self.cfg.get("max_retries", 50)
        self.retry_delay_s = self.cfg.get("retry_delay_s", 0.1)
        self.settle_after_read_s = self.cfg.get("settle_after_read_s", 0.05)
        self.align_poll_s = self.cfg.get("align_poll_s", 2.0)

    def apply(self, **params) -> dict:
        # The reference level is in the SA unit, so the unit (and the scale, which can
        # change the unit) goes first: "unit W, ref level 2e-8" means 2e-8 W.
        rank = {"scale": 1, "unit": 2, "ref_level": 3}
        ordered = dict(sorted(params.items(), key=lambda kv: rank.get(kv[0], 0)))
        # settings are idempotent, so after a garbled command all of them are sent again
        for attempt in range(1, APPLY_ATTEMPTS + 1):
            try:
                return super().apply(**ordered)
            except InstrumentError as e:
                if attempt == APPLY_ATTEMPTS or not garbled(e.errors):
                    raise
                log.warning("%s: apply(%s) got %s (a garbled transfer?); sending it again (%d/%d)",
                            self.name, ordered, e.errors, attempt + 1, APPLY_ATTEMPTS)

    def check_limit(self, key, value):
        super().check_limit(key, value)
        if key == "scale" and str(value).upper()[:3] not in self.SCALES:
            raise InstrumentError(f"{self.name}: scale must be LOG or LIN, not {value!r}")
        if key == "ref_level" and value is not None and self.limits.get("ref_level_dbm"):
            # limit in dBm, whatever the unit (the one in this apply(), or the SA's now)
            unit = self._pending.get("unit") or self.t.query(":UNIT:POW?").strip()
            dbm = level_to_dbm(float(value), unit)
            self.check_range("ref_level_dbm", dbm, f"ref_level={float(value):g} {unit} ({dbm:.2f} dBm)")

    # ---- setters ----
    def set_center(self, hz):
        self.t.write(f":FREQ:CENT {fmt(hz)}")
        self._axis = None

    def set_span(self, hz):
        self.t.write(f":FREQ:SPAN {fmt(hz)}")
        self._axis = None

    def set_rbw(self, hz):
        self.t.write(f":BAND {fmt(hz)}")
        self._axis = None

    def set_vbw(self, hz):
        self.t.write(f":BAND:VID {fmt(hz)}")
        self._axis = None

    def set_detector(self, det):
        self.t.write(f":DET:FUNC {det}")

    def set_ref_level(self, level):
        self.t.write(f"DISP:WIND:TRAC:Y:RLEV {fmt(level)}")

    def set_unit(self, unit):
        self.t.write(f":UNIT:POW {unit}")
        self._axis = None

    def set_scale(self, kind):
        """Vertical scale, separate from the unit: LOG (10 dB/div) or LIN (linear in
        voltage). In LIN our E7405A only reads right from about the reference level down
        to 20 dB below it: above it clips, below it the trace sits on a floor at about
        ref - 20 dB (seen 2026-10-14). LOG covers the full display range.

        The E7405A also changes the unit with the scale (LIN -> LOG turned DBM into
        DBUV, 2026-10-14), so the unit is put back: scale and unit stay separate."""
        kind = str(kind).upper()[:3]
        unit = self.t.query(":UNIT:POW?").strip()
        self.t.write(f":DISP:WIND:TRAC:Y:SPAC {kind}")
        if self.t.query(":UNIT:POW?").strip() != unit:
            self.t.write(f":UNIT:POW {unit}")
        self._axis = None

    def set_sweep_time(self, seconds):
        """None = auto (not in zero span: the E7405A answers +620)."""
        if seconds is None:
            self.t.write(":SWE:TIME:AUTO ON")
        else:
            self.t.write(f":SWE:TIME {fmt(seconds)}")
        self._axis = None

    def set_continuous(self, on):
        self.t.write(f":INIT:CONT {'ON' if on else 'OFF'}")
        self._continuous = bool(on)

    def set_attenuation(self, db):
        self.t.write(f":POW:ATT {fmt(db)}")

    def set_average_type(self, kind):
        """POW (power averaging, what the notebooks use), LOG, VOLT."""
        self.t.write(f":AVER:TYPE {kind}")

    def configure_zero_span(self, center_hz, rbw_hz, vbw_hz, detector="SAMP"):
        """Same sequence as twpa_calibration.ipynb (cell 14)."""
        self.set_detector(detector)
        self.set_vbw(vbw_hz)
        self.set_rbw(rbw_hz)
        self.set_center(center_hz)
        self.set_span(0)

    # ---- state ----
    def get_state(self) -> dict:
        q = lambda cmd: self.t.query(cmd).strip()
        state = {
            "center_hz": float(q(":SENS:FREQ:CENT?")),
            "span_hz": float(q(":SENS:FREQ:SPAN?")),
            "rbw_hz": float(q(":SENS:BAND?")),
            "vbw_hz": float(q(":SENS:BAND:VID?")),
            "detector": q(":SENS:DET:FUNC?"),
            "ref_level": float(q(":DISP:WIND:TRAC:Y:SCAL:RLEV?")),
            "unit": q(":UNIT:POW?"),
            "scale": q(":DISP:WIND:TRAC:Y:SPAC?").upper()[:3],
            "sweep_time_s": float(q(":SENS:SWE:TIME?")),
            "sweep_time_auto": q(":SWE:TIME:AUTO?") in ("1", "ON"),     # noise-sweep-zero-power.ipynb
            "continuous": q(":INIT:CONT?") in ("1", "ON"),
            "single_sweep": self.single_sweep,
            "attenuation_db": float(q(":POW:ATT?")),
            "average_type": q(":AVER:TYPE?"),
            # In single sweep, INIT:IMM runs the whole average count before *OPC?
            # answers, so a read takes sweep_time_s * average_count (assumed: the SA's
            # own averaging has not been used on our instrument yet).
            "average": self.query_optional(":SENS:AVER:STAT?", "0") in ("1", "ON"),
            "average_count": int(float(self.query_optional(":SENS:AVER:COUN?", "1"))),
            "alignment": self.alignment_status(),
        }
        self._continuous = state["continuous"]
        self._axis = {k: state[k] for k in ("center_hz", "span_hz", "rbw_hz", "vbw_hz", "sweep_time_s", "unit",
                                            "scale", "average", "average_count")}
        return self.remember_state(state)

    def _get_axis(self) -> dict:
        """Sweep geometry for building the x axis. Cached so live traces cost one query
        per frame; any setter invalidates it. Front-panel changes are picked up on the
        next get_state()."""
        if self._axis is None:
            self.get_state()
        return self._axis

    def forget_cached_state(self):
        self._axis = None

    def expected_sweep_s(self) -> float:
        """How long one read_trace() keeps the instrument busy."""
        ax = self._get_axis()
        return ax["sweep_time_s"] * (max(1, ax["average_count"]) if ax["average"] else 1)

    # ---- measurements ----
    def _fetch(self) -> np.ndarray:
        return self.t.query_ascii_values("TRAC? TRACE1")

    def _single_sweep(self) -> np.ndarray:
        if self.single_sweep and self._continuous is not False:
            self.set_continuous(False)
        self.t.write("INIT:IMM")
        self.t.query("*OPC?")
        y = self._fetch()
        if len(y) != N_POINTS or np.isnan(y).any():
            raise MeasurementError(f"bad trace (len={len(y)}, nan={np.isnan(y).any()})")
        return y

    def read_trace(self, max_retries: int | None = None) -> Trace:
        """Trigger one sweep and return it. Retries like the notebooks do
        (max_retries overrides the configured count, e.g. for the live view)."""
        ax = self._get_axis()
        with self.t.timeout_at_least(self.expected_sweep_s() * 2000 + 2000):
            y = self.retry(self._single_sweep, what="trace read",
                           max_retries=max_retries or self.max_retries, delay_s=self.retry_delay_s)
        if ax["span_hz"] == 0:
            x, x_unit = np.linspace(0, ax["sweep_time_s"], N_POINTS), "s"
        else:
            half = ax["span_hz"] / 2
            x, x_unit = np.linspace(ax["center_hz"] - half, ax["center_hz"] + half, N_POINTS), "Hz"
        return Trace(x=x, y=y, x_unit=x_unit, y_unit=ax["unit"],
                     meta={"center_hz": ax["center_hz"], "span_hz": ax["span_hz"],
                           "rbw_hz": ax["rbw_hz"], "vbw_hz": ax["vbw_hz"], "scale": ax["scale"]})

    def measure_power(self) -> dict:
        """Zero-span mean/std power in W (twpa_calibration.ipynb cells 15-17)."""
        ax = self._get_axis()
        if ax["span_hz"] != 0:
            raise MeasurementError(f"{self.name}: measure_power needs zero span (span={ax['span_hz']})")
        tr = self.read_trace()
        unit = tr.y_unit.upper()
        if unit == "W":
            p = tr.y
        elif unit == "DBM":
            p = dbm_to_w(tr.y)
        else:
            raise MeasurementError(f"{self.name}: cannot convert unit {tr.y_unit!r} to W")
        if self.settle_after_read_s:
            time.sleep(self.settle_after_read_s)
        return {"mean_w": float(np.mean(p)), "std_w": float(np.std(p)), "n_points": int(len(p)),
                "timestamp": tr.timestamp, **tr.meta}

    # ---- alignment ----
    def alignment_status(self) -> dict:
        return decode_alignment(int(float(self.query_optional(":STAT:QUES:CAL:COND?", "-1"))))

    def align(self, progress=None, timeout_s: float = 900.0) -> dict:
        """Align Now, All (:CAL:ALL). Without the cable from AMPTD REF OUT to INPUT the
        SA skips the RF part (+652 "Connect Calibration Output to Input") and aligns the
        rest (IF filters, LO, ADC).

        Takes about 3-6 min on our E7405A (2026-10-14). The SA answers status queries
        meanwhile but a trace read would hang, so the caller must keep the live view off
        (a claim does). Polls STAT:OPER:COND? until the calibrating bit clears."""
        self.discard_old_errors()
        before = self.alignment_status()
        self.t.write(":CAL:ALL")
        t0 = time.monotonic()
        seen, missed = False, 0
        while True:
            time.sleep(self.align_poll_s)
            elapsed = time.monotonic() - t0
            try:
                busy = int(float(self.t.query(":STAT:OPER:COND?"))) & OPER_CALIBRATING
                missed = 0
            except TransportError as e:         # slow answers while it aligns: keep waiting
                missed += 1
                log.warning("%s: status query during alignment failed (%d): %s", self.name, missed, e)
                busy = True
            seen = seen or bool(busy)
            if progress:
                progress(elapsed)
            if not busy and (seen or elapsed > 20):
                break
            if elapsed > timeout_s:
                raise MeasurementError(f"{self.name}: alignment still running after {timeout_s:g} s")
        errors = self.errors()
        self._axis = None
        self._continuous = None         # the next read sets single sweep again
        return {"duration_s": round(time.monotonic() - t0, 1), "before": before,
                "after": self.alignment_status(),
                "rf_skipped_no_cable": any(e.startswith(CAL_NEEDS_CABLE) for e in errors),
                "errors": [e for e in errors if not e.startswith(CAL_NEEDS_CABLE)]}

    def idle(self):
        if self.single_sweep and self._continuous is False:
            self.set_continuous(True)

    def close(self):
        if self.single_sweep:
            try:
                self.set_continuous(True)
            except Exception:
                pass
        super().close()


class SimE7405A(SimTransport):
    IDN = "Hewlett-Packard,E7405A,SIM00000,A.14.04"
    UNITS = ("DBM", "W", "V", "DBMV", "DBUV")
    DETECTORS = ("POS", "SAMP", "NEG", "NORM", "AVER", "RMS")

    def __init__(self, world=None):
        super().__init__(world)
        self.s = dict(center=6.6e9, span=13.2e9, rbw=3e6, vbw=3e6, det="POS", rlev=0.0,
                      unit="DBM", scale="LOG", swe_time=None, cont=True, form="ASC",
                      att=10.0, avg_type="LOG", align=True, rbw_auto=True, vbw_auto=True,
                      avg=False, avg_count=100)
        self._last = None

    def sweep_time(self) -> float:
        if self.s["swe_time"] is not None:
            return self.s["swe_time"]
        if self.s["span"] == 0:
            return 0.025
        return max(0.005, 2.5 * self.s["span"] / self.s["rbw"] ** 2)

    def _num(self, key, q, args):
        if q:
            return f"{self.s[key]:.10g}"
        self.s[key] = parse_number(args)

    @scpi("[SENSe]:FREQuency:CENTer")
    def _center(self, q, a): return self._num("center", q, a)

    @scpi("[SENSe]:FREQuency:SPAN")
    def _span(self, q, a):
        if q:
            return f"{self.s['span']:.10g}"
        span = parse_number(a)
        if span == 0 and self.s["swe_time"] is None:
            self.s["swe_time"] = self.sweep_time()      # zero span has no auto: the last value stays, manual
        self.s["span"] = span
        self._couple()

    def _couple(self):
        """Auto-coupled bandwidths, as on the ESA: RBW ~ span/106, VBW = RBW."""
        if self.s["rbw_auto"] and self.s["span"] > 0:
            self.s["rbw"] = self._snap_1_3_10(self.s["span"] / 106, 1e3, 3e6)
        if self.s["vbw_auto"]:
            self.s["vbw"] = self._snap_1_3_10(self.s["rbw"], 1, 3e6)

    @scpi("[SENSe]:FREQuency:STARt")
    def _start(self, q, a):
        if q:
            return f"{self.s['center'] - self.s['span'] / 2:.10g}"
        stop = self.s["center"] + self.s["span"] / 2
        start = parse_number(a)
        self.s["center"], self.s["span"] = (start + stop) / 2, stop - start

    @scpi("[SENSe]:FREQuency:STOP")
    def _stop(self, q, a):
        if q:
            return f"{self.s['center'] + self.s['span'] / 2:.10g}"
        start = self.s["center"] - self.s["span"] / 2
        stop = parse_number(a)
        self.s["center"], self.s["span"] = (start + stop) / 2, stop - start

    @staticmethod
    def _snap_1_3_10(hz, lo, hi):
        """ESA-E bandwidths come in 1-3-10 steps; the instrument picks the nearest."""
        steps = [m * 10 ** e for e in range(0, 8) for m in (1, 3) if lo <= m * 10 ** e <= hi]
        return min(steps, key=lambda v: abs(np.log(v / hz)))

    @scpi("[SENSe]:BANDwidth[:RESolution]")
    def _rbw(self, q, a):
        if q:
            return f"{self.s['rbw']:.10g}"
        self.s["rbw"] = self._snap_1_3_10(parse_number(a), 1e3, 3e6)
        self.s["rbw_auto"] = False
        self._couple()

    @scpi("[SENSe]:BANDwidth:VIDeo")
    def _vbw(self, q, a):
        if q:
            return f"{self.s['vbw']:.10g}"
        self.s["vbw"] = self._snap_1_3_10(parse_number(a), 1, 3e6)
        self.s["vbw_auto"] = False

    @scpi("[SENSe]:BANDwidth[:RESolution]:AUTO")
    def _rbw_auto(self, q, a):
        if q:
            return "1" if self.s["rbw_auto"] else "0"
        self.s["rbw_auto"] = parse_bool(a)
        self._couple()

    @scpi("[SENSe]:BANDwidth:VIDeo:AUTO")
    def _vbw_auto(self, q, a):
        if q:
            return "1" if self.s["vbw_auto"] else "0"
        self.s["vbw_auto"] = parse_bool(a)
        self._couple()

    @scpi("[SENSe]:DETector[:FUNCtion]")
    def _det(self, q, a):
        if q:
            return self.s["det"]
        det = a.upper()
        if det not in self.DETECTORS:
            raise ValueError(det)
        self.s["det"] = det

    @scpi("DISPlay:WINDow:TRACe:Y[:SCALe]:RLEVel")
    def _rlev(self, q, a): return self._num("rlev", q, a)

    @scpi("UNIT:POWer")
    def _unit(self, q, a):
        if q:
            return self.s["unit"]
        if a.upper() not in self.UNITS:
            raise ValueError(a)
        self.s["unit"] = a.upper()

    @scpi("DISPlay:WINDow:TRACe:Y[:SCALe]:SPACing")
    def _scale(self, q, a):
        if q:
            return self.s["scale"]
        kind = a.upper()[:3]
        if kind not in ("LOG", "LIN"):
            raise ValueError(a)
        if kind != self.s["scale"]:
            # the real one changes the unit with the scale (LIN -> LOG gave DBUV); LOG -> LIN assumed V
            self.s["unit"] = "DBUV" if kind == "LOG" else "V"
        self.s["scale"] = kind

    @scpi("[SENSe]:SWEep:TIME")
    def _swe_time(self, q, a):
        if q:
            return f"{self.sweep_time():.10g}"
        self.s["swe_time"] = parse_number(a)

    @scpi("[SENSe]:SWEep:TIME:AUTO")
    def _swe_auto(self, q, a):
        if q:
            return "1" if self.s["swe_time"] is None else "0"
        if parse_bool(a):
            if self.s["span"] == 0:
                self.push_error(620, "Can't Auto-Couple Sweep Time in Zero Span")    # as our E7405A
                return
            self.s["swe_time"] = None

    @scpi("INITiate:CONTinuous")
    def _cont(self, q, a):
        if q:
            return "1" if self.s["cont"] else "0"
        self.s["cont"] = parse_bool(a)

    @scpi("INITiate[:IMMediate]")
    def _init(self, q, a):
        if time.monotonic() < getattr(self, "_cal_until", 0.0):
            raise TransportError("INIT:IMM during alignment: timeout (sim: the real one hangs)")
        sweeps = self.s["avg_count"] if self.s["avg"] else 1
        self.start_operation(self.sweep_time() * sweeps)
        self._last = self._generate()

    @scpi("FORMat[:TRACe][:DATA]")
    def _form(self, q, a):
        if q:
            return "ASC,8" if self.s["form"] == "ASC" else "REAL,32"
        f = a.replace(" ", "").upper()
        if f in ("ASC", "ASCII", "ASC,8"):
            self.s["form"] = "ASC"
        elif f == "REAL,32":
            self.s["form"] = "REAL"
        else:
            raise ValueError(a)

    @scpi("[SENSe]:POWer[:RF]:ATTenuation")
    def _att(self, q, a): return self._num("att", q, a)

    @scpi("[SENSe]:AVERage:TYPE")
    def _avg_type(self, q, a):
        if q:
            return self.s["avg_type"]
        if a.upper()[:3] not in ("POW", "LOG", "VOL", "SCA"):
            raise ValueError(a)
        self.s["avg_type"] = a.upper()[:3]

    @scpi("[SENSe]:AVERage[:STATe]")
    def _avg(self, q, a):
        if q:
            return "1" if self.s["avg"] else "0"
        self.s["avg"] = parse_bool(a)

    @scpi("[SENSe]:AVERage:COUNt")
    def _avg_count(self, q, a):
        if q:
            return str(self.s["avg_count"])
        n = int(parse_number(a))
        if not 1 <= n <= 8192:
            raise ValueError(a)
        self.s["avg_count"] = n

    @scpi("CALibration:AUTO")          # E7405A; newer ESAs use CAL:ALIGn:AUTO
    def _align(self, q, a):
        if q:
            return "1" if self.s["align"] else "0"
        self.s["align"] = parse_bool(a)

    ALIGN_S = 30.0      # ours takes about 5 min; shorter here so trying it in --sim isn't a long wait
    CAL_NO_CABLE = CAL_RF_SKIPPED | CAL_RF_NEEDED | (1 << 3)     # what ours reported after it (6152)

    @scpi("CALibration[:ALL]")
    def _cal_all(self, q, a):
        """Like ours without the AMPTD REF OUT cable: aligns the rest, skips RF, +652."""
        self._cal_until = time.monotonic() + self.ALIGN_S * self.world.time_scale
        self._cal_polls = 1         # the first status query shows it busy even with time_scale 0
        self.push_error(652, "Connect Calibration Output to Input")
        self.s["cal_cond"] = self.CAL_NO_CABLE
        if q:
            self._cal_until = 0.0   # the query form waits until done (and needs a long timeout)
            return "0"

    def _calibrating(self) -> bool:
        if getattr(self, "_cal_polls", 0) > 0:
            self._cal_polls -= 1
            return True
        return time.monotonic() < getattr(self, "_cal_until", 0.0)

    @scpi("STATus:OPERation:CONDition")
    def _oper_cond(self, q, a):
        return str((OPER_CALIBRATING if self._calibrating() else 0) | (0 if self.s["cont"] else 8))

    @scpi("STATus:QUEStionable:CALibration:CONDition")
    def _cal_cond(self, q, a):
        return f"+{self.s.get('cal_cond', 0)}"

    @scpi("TRACe[:DATA]")
    def _trace(self, q, a):
        if self.world.rng.random() < self.world.fail_rate:
            if self.world.rng.integers(2):
                raise TransportError("TRAC?: timeout (sim: injected failure)")
            y = self._last.copy() if self._last is not None else self._generate()
            y[self.world.rng.integers(N_POINTS)] = np.nan
            return SimBlock(y, binary=self.s["form"] == "REAL")
        y = self._last if self._last is not None else self._generate()
        return SimBlock(y, binary=self.s["form"] == "REAL")

    def _generate(self) -> np.ndarray:
        s, rng = self.s, self.world.rng
        if s["span"] == 0:
            freqs = np.full(N_POINTS, s["center"])
        else:
            freqs = np.linspace(s["center"] - s["span"] / 2, s["center"] + s["span"] / 2, N_POINTS)
        p = self.world.spa_power_w(freqs, s["rbw"])
        rel_std = float(np.clip(np.sqrt(s["vbw"] / s["rbw"]), 0.005, 1.0))
        p = p * np.abs(1 + rel_std * rng.standard_normal(N_POINTS))
        if s["unit"] == "W":
            return p
        return 10 * np.log10(p / 1e-3)


E7405A.SIM = SimE7405A
