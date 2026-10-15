"""Y factor measurement: SA noise power vs noise source bias, for a Y-factor analysis later.

Steps the noise source bias (33220A DC offset, the hot wire) and records the zero-span
noise power on the SA at one frequency at each step, like noise-sweep-zero-power.ipynb
(-0.2..0.2 V in 41 points, RBW 3 MHz, sample detector, power averaging, unit W, the mean
and std of one trace per point). This only takes the data; T_N and gain are fitted
separately from power_w(bias_v).

Signal path: fridge_in -> rf_probe, rx_source -> fridge, receiver -> spa. The SynthHD
probe is turned off first, so nothing drives the fridge input (the VNA is switched out).

TWPA pump (SynthHD channel B): left as it is, set to a frequency and power and turned on
for the sweep (TWPA on), or turned off for the sweep (TWPA off, the reference). Its
state during the sweep is saved.

Qubit flux bias: left as it is, or set to a fixed voltage for the sweep (to move the
qubit away from the measurement frequency, or to park it there on purpose). It is
reserved either way, so nothing changes it during the sweep.

Noise source: if its output is off it is set to DC at the first point and then turned
on; if it is on it must already be in DC mode. Passes > 1 repeat the sweep, every other
pass in reverse, so drifts show up as hysteresis.

Result: power_w[point] (mean of every sample of every trace at that point), with the
mean/std of each trace, an XY plot in the UI.
"""
from __future__ import annotations

import logging
import time

import numpy as np

from ..drivers.base import within
from ..drivers.e7405a import N_POINTS, dbm_to_w
from .base import Experiment, Param
from .qubit_flux_sweep import FLUX, flux_finish, flux_setup, flux_start
from .vna_sweep import sweep_values

log = logging.getLogger("labserver.experiments")

NOISE = "noise_source_bias"
SYNTH = "rf_synth"
PROBE_ON = "probe_enable"
PUMP_ON = "pump_enable"
PUMP_KEYS = ("pump_frequency_hz", "pump_power_dbm")
SWITCH_PATH = {"fridge_in": "rf_probe", "rx_source": "fridge", "receiver": "spa"}
TRACE_OVERHEAD_S = 0.3      # INIT:IMM + *OPC? + 401-point ASCII transfer over GPIB (rough)
MIN_SWEEP_S = 0.01          # floor for the default sweep time (the E7405A can sweep faster)
SA_KEYS = ("detector", "vbw_hz", "rbw_hz", "center_hz", "span_hz", "sweep_time_s", "scale", "unit", "ref_level")

SWEEP = "Noise source bias sweep"
SA = "Spectrum analyzer (zero span)"
PUMP = "TWPA pump"
QUBIT = "Qubit flux bias"
END = "Before and after"
SAVE = "Save"


def default_sweep_time(rbw_hz: float, vbw_hz: float) -> float:
    """Zero-span sweep time with the 401 samples about two video time constants apart.

    The detected noise is smoothed by the narrower of the RBW filter (envelope) and the
    VBW filter; with a single-pole time constant tau = 1/(2 pi B) the samples correlate
    as exp(-dt/tau), so dt = 2 tau = 1/(pi B) leaves e^-2 ~ 0.14 between neighbours.
    Sweep time = 400 dt, rounded to 2 digits, at least MIN_SWEEP_S."""
    b = min(rbw_hz, vbw_hz)
    return max(MIN_SWEEP_S, float(f"{400 / (np.pi * b):.2g}"))


def pump_text(st: dict) -> str:
    return (f"on at {st['pump_frequency_hz'] / 1e9:g} GHz, {st['pump_power_dbm']:g} dBm"
            if st.get(PUMP_ON) else "off")


def bias_setup(state: dict, first: float) -> tuple[str | None, str]:
    """(error or None, note) for sweeping the noise source from first."""
    func = str(state.get("function", "")).upper()
    if state["output"] and not func.startswith("DC"):
        return (f"the noise source bias output is on in {func or '?'} mode; turn it off "
                f"(the sweep then sets DC) or set it to DC"), ""
    if state["output"]:
        return None, f"noise source bias output is on at {state['offset_v']:g} V; set to {first:g} V"
    return None, f"noise source bias output is off: set to DC {first:g} V, then turned on"


class YFactorMeasurement(Experiment):
    id = "y_factor_measurement"
    title = "Y factor measurement"
    summary = ("Step the noise source bias (33220A DC, hot wire) and record the zero-span noise power "
               "on the SA at one frequency at each step, for a Y-factor analysis afterwards. "
               "Result: power vs bias.")
    instruments = ["spa", NOISE, "warm_switches", SYNTH, FLUX]
    turns_on = "the noise source bias (and the TWPA pump and the flux bias, if set for the sweep)"
    params = [
        Param("bias_start_v", "from", unit="V", default=-0.2, group=SWEEP),
        Param("bias_stop_v", "to", unit="V", default=0.2, group=SWEEP),
        Param("bias_step_v", "step", unit="V", default=0.01, group=SWEEP,
              help="33220A DC offset (the nominal value it shows). Limit: noise_source_bias DC offset "
                   "in the settings tab. The hot-wire noise power goes as V^2, so +-V give the same point."),
        Param("traces", "traces per point", kind="int", default=1, group=SWEEP,
              help="Zero-span traces (401 samples each) averaged at each bias."),
        Param("settle_s", "wait after each bias step", unit="s", default=0.05, group=SWEEP,
              help="Lets the hot wire settle before the first trace."),
        Param("passes", "passes", kind="int", default=1, group=SWEEP,
              help="Repeat the sweep; every other pass goes the other way, so drifts show up as hysteresis."),
        # SA
        Param("center_hz", "frequency", unit="GHz", scale=1e9, default=5.58e9, group=SA,
              from_state=["spa", "center_hz"]),
        Param("rbw_hz", "RBW", unit="kHz", scale=1e3, default=3e6, group=SA, from_state=["spa", "rbw_hz"],
              help="The ESA takes 1-3-10 steps (RBW and VBW): the run reads back the values it took, works out "
                   "the default sweep time from them, and saves and shows them."),
        Param("vbw_hz", "VBW", unit="kHz", scale=1e3, default=1e3, group=SA, from_state=["spa", "vbw_hz"]),
        Param("sweep_time_s", "sweep time", unit="s", default=None, group=SA, from_state=["spa", "sweep_time_s"],
              help="Length of one zero-span trace (401 points). Empty = from the RBW/VBW the SA takes (shown in gray): "
                   "400 / (pi min(RBW, VBW)), so neighbouring samples are about two video time constants "
                   "apart and nearly independent; at least 10 ms. Zero span has no auto sweep time on the "
                   "E7405A (+620), so the value is always sent."),
        Param("scale", "vertical scale", kind="select", default="LOG", group=SA,
              options=[["LOG", "log (10 dB/div)"], ["LIN", "linear"]],
              help="Separate from the unit (the sweep always reads in W). In LIN our E7405A only reads right "
                   "from the reference level down to about 20 dB below it: above it clips, below it the "
                   "trace sits on a floor near ref - 20 dB. LOG has no such window. Restored when done."),
        Param("detector", "detector", kind="select", default="SAMP", group=SA,
              options=[["SAMP", "sample (as in the notebooks)"], ["AVER", "average (not tried on our ESA yet)"]]),
        Param("ref_level_unit", "reference level in", kind="select", default="dbm", group=SA,
              options=[["dbm", "dBm"], ["w", "W"]],
              help="Keep it above the noise power at the highest bias, or the SA clips. The sweep "
                   "reads in W, so it is sent as W either way (-45 dBm = 31.6 nW)."),
        Param("ref_level_dbm", "reference level", unit="dBm", default=-45.0, group=SA,
              show_if={"ref_level_unit": ["dbm"]}),
        Param("ref_level_w", "reference level", unit="W", default=2e-8, group=SA,
              show_if={"ref_level_unit": ["w"]}, from_state=["spa", "ref_level"],
              help="e.g. 2e-8 (as in noise-sweep-zero-power.ipynb). Load current values reads the SA's "
                   "reference level in its present unit, so the SA must be in W for that."),
        Param("save_samples", "save every sample (not only the trace means)", kind="bool", default=False, group=SA),
        # TWPA pump
        Param("pump_mode", "TWPA pump", kind="select", default="keep", group=PUMP,
              options=[["keep", "leave it as it is"], ["set", "set it and turn it on for the sweep (TWPA on)"],
                       ["off", "turn it off for the sweep (TWPA off)"]],
              help="SynthHD channel B. Its state during the sweep is saved either way, so a TWPA-on and a "
                   "TWPA-off run can be told apart afterwards."),
        Param("pump_frequency_hz", "pump frequency", unit="GHz", scale=1e9, default=8.5e9, group=PUMP,
              show_if={"pump_mode": ["set"]}, from_state=[SYNTH, "pump_frequency_hz"],
              help="Limit: rf_synth pump frequency in the settings tab."),
        Param("pump_power_dbm", "pump power", unit="dBm", default=7.0, group=PUMP,
              show_if={"pump_mode": ["set"]}, from_state=[SYNTH, "pump_power_dbm"],
              help="Limit: rf_synth pump power in the settings tab. Set before the output is turned on."),
        Param("pump_settle_s", "wait after the pump", unit="s", default=1.0, group=PUMP,
              show_if={"pump_mode": ["set", "off"]},
              help="Before the first bias point, so the TWPA (and the fridge) settle with the pump on or off."),
        # qubit
        Param("flux_mode", "qubit flux bias", kind="select", default="keep", group=QUBIT,
              options=[["keep", "leave it as it is"], ["set", "set it for the sweep"]],
              help="If the qubit sits at the measurement frequency it shapes the noise there: move it "
                   "away, or keep it there on purpose. Either way it is reserved during the sweep and "
                   "its state is saved."),
        Param("flux_bias_v", "flux bias", unit="V", default=0.0, group=QUBIT, show_if={"flux_mode": ["set"]},
              from_state=[FLUX, "level"],
              help="Limit: qubit_flux_bias level (VOLT mode) in the settings tab. If the output is off it is turned "
                   "on at 0 V and ramped there; if on, ramped from where it is."),
        # before / after
        Param("set_switches", "set the warm switches to SynthHD probe -> fridge -> SA first", kind="bool",
              default=True, group=END,
              help="fridge_in -> rf_probe, rx_source -> fridge, receiver -> spa. The SynthHD probe is "
                   "turned off either way; the TWPA pump only as set above."),
        Param("finish", "when done", kind="select", default="restore", group=END,
              options=[["restore", "restore the SA, switches, SynthHD probe and pump, flux bias and noise source "
                                   "as they were"],
                       ["last", "leave everything at the last point (the probe stays off)"],
                       ["off", "turn the noise source off at 0 V, and the TWPA pump and the flux bias "
                               "(ramped to 0 V) if the sweep set them; the SA and switches stay"]]),
        # save
        Param("fridge_temperature_k", "fridge temperature (for the record)", unit="K", default=None, group=SAVE),
        Param("label", "label", kind="text", default="y-factor", group=SAVE),
        Param("note", "note", kind="text", default="", group=SAVE),
    ]

    # ---- plan ----
    def points(self, p: dict) -> tuple[list[tuple[int, float]], list[str], list[str]]:
        """[(pass, bias)] in sweep order, plus (errors, warnings)."""
        errors, warnings = [], []
        start, stop, step = p["bias_start_v"], p["bias_stop_v"], p["bias_step_v"]
        if step <= 0:
            errors.append("bias step must be > 0")
        elif abs(stop - start) / step + 1 > self.cap("max_points"):
            errors.append(f"more than {self.cap('max_points')} bias points; use a larger step")
        if p["traces"] < 1:
            errors.append("traces per point must be >= 1")
        if p["passes"] < 1:
            errors.append("passes must be >= 1")
        if errors:
            return [], errors, warnings
        values, warn = sweep_values(start, stop, step, "V")
        if warn:
            warnings.append(warn)
        points = [(k, v) for k in range(p["passes"]) for v in (values if k % 2 == 0 else values[::-1])]
        if len(points) > self.cap("max_total_points"):
            errors.append(f"more than {self.cap('max_total_points')} points in all; use fewer passes or a larger step")
        return points, errors, warnings

    @staticmethod
    def sweep_time(p: dict) -> float | None:
        """The typed sweep time, or the default from RBW/VBW (None if those are invalid)."""
        if p["sweep_time_s"] is not None:
            return p["sweep_time_s"]
        if p["rbw_hz"] > 0 and p["vbw_hz"] > 0:
            return default_sweep_time(p["rbw_hz"], p["vbw_hz"])
        return None

    @staticmethod
    def ref_level_w(p: dict) -> float:
        return p["ref_level_w"] if p["ref_level_unit"] == "w" else float(dbm_to_w(p["ref_level_dbm"]))

    def plan(self, p: dict, mgr=None) -> dict:
        points, errors, warnings = self.points(p)
        notes = []
        if p["ref_level_unit"] == "w" and not p["ref_level_w"] > 0:
            errors.append("reference level in W must be > 0 (if it came from load current values, "
                          "the SA was not in W)")
        self.check_limit(errors, NOISE, "offset_v", [v for _, v in points], "noise source bias", "V")
        if p["rbw_hz"] <= 0 or p["vbw_hz"] <= 0:
            errors.append("RBW and VBW must be > 0")
        if p["sweep_time_s"] is not None and p["sweep_time_s"] <= 0:
            errors.append("sweep time must be > 0; leave it empty for the default from RBW/VBW "
                          "(zero span has no auto sweep time)")
        if p["scale"] == "LIN":
            warnings.append("LIN scale: the SA reads right only from the reference level down to about "
                            "20 dB below it; LOG is safer")
        if p["settle_s"] < 0:
            errors.append("wait after each step must be >= 0")
        if p["center_hz"] <= 0:
            errors.append("frequency must be > 0")
        if p["flux_mode"] == "set":
            self.check_limit(errors, FLUX, "level", p["flux_bias_v"], "flux bias", "V")
        if p["pump_mode"] == "set":
            self.check_limit(errors, SYNTH, "pump_frequency_hz", p["pump_frequency_hz"], "pump frequency", "Hz")
            self.check_limit(errors, SYNTH, "pump_power_dbm", p["pump_power_dbm"], "pump power", "dBm")
            if not 10e6 <= p["pump_frequency_hz"] <= 15e9:
                errors.append("pump frequency must be within 0.01..15 GHz (SynthHD)")
            if not -80 <= p["pump_power_dbm"] <= 20:
                errors.append("pump power must be within -80..20 dBm (SynthHD)")
        if p["pump_mode"] != "keep" and p["pump_settle_s"] < 0:
            errors.append("wait after the pump must be >= 0")
        # what the run sends to the SA (the reference level limit is in dBm, the run sends W)
        for key, value, what, unit in (("center_hz", p["center_hz"], "SA frequency", "Hz"),
                                       ("span_hz", 0, "SA span (zero span)", "Hz"),
                                       ("rbw_hz", p["rbw_hz"], "RBW", "Hz"), ("vbw_hz", p["vbw_hz"], "VBW", "Hz"),
                                       ("sweep_time_s", self.sweep_time(p), "sweep time", "s")):
            if value is not None:
                self.check_limit(errors, "spa", key, value, what, unit)
        if self.ref_level_w(p) > 0:
            self.check_limit(errors, "spa", "ref_level_dbm", round(10 * np.log10(self.ref_level_w(p) * 1e3), 2),
                             "SA reference level", "dBm")

        sa_avg = 1
        if mgr is not None:
            sa_avg = self.check_instruments(mgr, p, points, errors, warnings, notes)
        traces = len(points) * p["traces"]
        sweep_s = self.sweep_time(p)
        trace_s = max(sweep_s or 0, 0) * sa_avg + TRACE_OVERHEAD_S
        estimate_s = (len(points) * p["settle_s"] + traces * trace_s
                      + (max(p["pump_settle_s"], 0) if p["pump_mode"] != "keep" else 0))
        size_mb = (len(points) * (2 * p["traces"] + 8)
                   + (traces * N_POINTS if p["save_samples"] else 0)) * 26 / 1e6
        derived = {}
        if p["sweep_time_s"] is None and sweep_s is not None:
            derived["sweep_time_s"] = sweep_s
            notes.append(f"sweep time {sweep_s:g} s per trace, from RBW {p['rbw_hz'] / 1e3:g} kHz / VBW {p['vbw_hz'] / 1e3:g} kHz "
                         f"(the run works it out again from the RBW/VBW the SA takes)")
        if sweep_s and sweep_s > 10:
            warnings.append(f"each trace takes {sweep_s:g} s; a wider VBW or a shorter sweep time is faster")
        if points:
            notes.append(f"{traces} traces in all at {p['center_hz'] / 1e9:g} GHz; noise source bias "
                         f"{points[0][1]:g} -> {points[-1][1]:g} V"
                         + (f", {p['passes']} passes back and forth" if p["passes"] > 1 else ""))
        return {"points": [{"i": i, "pass": k, "bias_v": v, "traces": p["traces"]}
                           for i, (k, v) in enumerate(points)],
                "estimate_s": round(estimate_s, 1), "size_mb": round(size_mb, 2),
                "columns": [["i", "#", ""], ["pass", "pass", ""], ["bias_v", "bias", "V"], ["traces", "traces", ""]],
                "derived": derived, "errors": errors, "warnings": warnings, "notes": notes}

    def check_instruments(self, mgr, p, points, errors, warnings, notes) -> int:
        """Reads only. Returns the sweeps per trace (the SA's own averaging)."""
        status = mgr.status()
        down = [n for n in self.instruments if n in status and not status[n]["connected"]]
        if down:
            warnings.append("not connected: " + ", ".join(down))

        sa_avg = 1
        try:
            sa = self.read_state(mgr, "spa")
            if sa.get("average"):
                sa_avg = max(1, int(sa.get("average_count", 1)))
                warnings.append(f"the SA's own averaging is on: each trace is {sa_avg} sweeps")
            if sa.get("attenuation_db"):
                notes.append(f"SA input attenuation is {sa['attenuation_db']:g} dB (not changed)")
        except Exception as e:
            warnings.append(f"could not read the SA: {e}")

        if points:
            try:
                err, note = bias_setup(self.read_state(mgr, NOISE), points[0][1])
                if err:
                    errors.append(err)
                else:
                    notes.append(note)
            except Exception as e:
                warnings.append(f"could not read the noise source bias: {e}")

        try:
            fl = self.read_state(mgr, FLUX)
            if p["flux_mode"] == "set":
                _, err, note = flux_setup(fl, [p["flux_bias_v"]])
                if err:
                    errors.append(err)
                else:
                    notes.append(note)
            else:
                notes.append("flux bias left as it is: "
                             + (f"on at {fl['level']:g} V" if fl["output"] else "output off"))
        except Exception as e:
            (errors if p["flux_mode"] == "set" else warnings).append(f"could not read the flux bias: {e}")

        try:
            sw = self.read_state(mgr, "warm_switches")
        except Exception as e:
            warnings.append(f"could not read the warm switches: {e}")
            self.check_sources(mgr, p, None, errors, warnings, notes)
            return sa_avg
        wrong = {k: sw.get(k) for k, v in SWITCH_PATH.items() if sw.get(k) != v}
        if not wrong:
            notes.append("warm switches are on the fridge -> SA path")
        elif p["set_switches"]:
            notes.append("will set warm switches: " + ", ".join(f"{k} {wrong[k]} -> {SWITCH_PATH[k]}" for k in wrong))
        else:
            warnings.append("warm switches are not on the fridge -> SA path: "
                            + ", ".join(f"{k}={v}" for k, v in wrong.items()))
        self.check_sources(mgr, p, sw.get("fridge_in"), errors, warnings, notes)
        return sa_avg

    def check_sources(self, mgr, p, fridge_in, errors, warnings, notes):
        """A tone into the fridge would land on the SA. The probe is turned off by the sweep;
        the VNA only matters if the switches are left on it."""
        try:
            synth = self.read_state(mgr, SYNTH)
            if synth.get(PROBE_ON):
                notes.append(f"SynthHD probe is on ({synth['probe_frequency_hz'] / 1e9:g} GHz, "
                             f"{synth['probe_power_dbm']:g} dBm): turned off first"
                             + (", back on after" if p["finish"] == "restore" else ""))
            notes.append(self.pump_note(p, synth))
        except Exception as e:
            (errors if p["pump_mode"] != "keep" else warnings).append(f"could not read the SynthHD: {e}")
        if fridge_in == "vna" and not p["set_switches"]:
            try:
                if self.read_state(mgr, "vna").get("output"):
                    warnings.append("the VNA RF output is on and switched into the fridge (fridge_in = vna)")
            except Exception as e:
                warnings.append(f"could not read the VNA: {e}")

    @staticmethod
    def pump_note(p: dict, synth: dict) -> str:
        now = pump_text(synth)
        back = ", back as it was after" if p["finish"] == "restore" else ""
        if p["pump_mode"] == "set":
            want = pump_text({PUMP_ON: True, **{k: p[k] for k in PUMP_KEYS}})
            if now == want:
                return f"TWPA pump is already {now}"
            return f"TWPA pump: {now} -> {want}" + (back or (", off after" if p["finish"] == "off" else ""))
        if p["pump_mode"] == "off":
            return "TWPA pump is already off" if not synth.get(PUMP_ON) else f"TWPA pump is {now}: turned off{back}"
        return f"TWPA pump left as it is: {now}"

    @staticmethod
    def read_state(mgr, name: str) -> dict:
        owner = mgr.slots[name].claimed_by or "plan"     # a running sweep holds it
        with mgr.use(name, owner=owner, timeout=2.0) as inst:
            return inst.get_state()

    # ---- run ----
    def run(self, p: dict, job) -> None:
        plan = self.plan(p)
        if plan["errors"]:
            raise ValueError("; ".join(plan["errors"]))
        points = [(pt["pass"], pt["bias_v"]) for pt in plan["points"]]
        mgr, owner = job.mgr, job.owner
        job.progress("reserving instruments", points_total=len(points), traces_total=len(points) * p["traces"])

        with mgr.claim(self.instruments, owner):
            snapshot = mgr.snapshot(owner=owner)
            for name in ("spa", NOISE, SYNTH) + ((FLUX,) if p["flux_mode"] == "set" else ()):
                if "error" in snapshot.get(name, {}):
                    raise RuntimeError(f"cannot read {name}: {snapshot[name]['error']}")
            err, _ = bias_setup(snapshot[NOISE], points[0][1])
            if err:
                raise RuntimeError(err)
            rec = job.record
            rec.update(kind="experiment", experiment=self.id, params=p, note=p["note"], sim=mgr.sim,
                       fridge_temperature_k=p["fridge_temperature_k"], frequency_hz=p["center_hz"],
                       bias_v=[], **{"pass": []}, traces=[], point_start_time=[], point_end_time=[],
                       power_w=[], power_std_w=[], trace_mean_w=[], trace_std_w=[], instruments=snapshot)
            if p["save_samples"]:
                rec["samples_w"] = []
            job.axis = {"plot": "xy", "x_key": "bias_v", "x_label": "noise source bias", "x_unit": "V",
                        "y_key": "power_w", "y_label": "noise power", "y_unit": "W"}
            try:
                self._measure(p, job, points, snapshot)
            finally:
                self._finish(p, job, snapshot)

    def _measure(self, p, job, points, snapshot):
        mgr, owner, rec = job.mgr, job.owner, job.record
        job.progress("turning the SynthHD probe off")
        mgr.apply(SYNTH, owner, **{PROBE_ON: False})
        if p["set_switches"]:
            job.progress("setting the warm switches")
            mgr.apply("warm_switches", owner, **SWITCH_PATH)
        self._pump_start(p, job)
        if p["flux_mode"] == "set":
            flux_start(job, snapshot[FLUX], [p["flux_bias_v"]])
        try:
            rec["flux_bias_state"] = mgr.get_state(FLUX, owner)       # during the sweep
        except Exception as e:
            job.notes.append(f"could not read the flux bias: {e}")

        job.progress("setting up the SA")
        # same order as twpa_calibration.ipynb's zero-span setup. The SA picks its own
        # RBW/VBW steps (1-3-10), so they are read back before anything depends on them:
        # the default sweep time is worked out from the values the SA took.
        # the scale goes before the unit, in case the SA changes the unit with it
        sa = mgr.apply("spa", owner, detector=p["detector"], vbw_hz=p["vbw_hz"], rbw_hz=p["rbw_hz"],
                       center_hz=p["center_hz"], span_hz=0, scale=p["scale"], unit="W")
        for key, want, label in (("rbw_hz", p["rbw_hz"], "RBW"), ("vbw_hz", p["vbw_hz"], "VBW")):
            if abs(sa[key] - want) > 1e-6 * want:
                job.notes.append(f"the SA took {label} {sa[key] / 1e3:g} kHz (asked for {want / 1e3:g} kHz)")
        sweep_s = p["sweep_time_s"] or default_sweep_time(sa["rbw_hz"], sa["vbw_hz"])
        ref_w = self.ref_level_w(p)
        # the unit (W) is set above, so the reference level is in W
        sa = mgr.apply("spa", owner, sweep_time_s=sweep_s, ref_level=ref_w)
        if sa["span_hz"] != 0:
            raise RuntimeError(f"SA span is {sa['span_hz']:g} Hz, not zero span")
        if str(sa["unit"]).upper() != "W":
            raise RuntimeError(f"SA unit is {sa['unit']}, not W")
        if sa.get("scale") != p["scale"]:
            raise RuntimeError(f"SA scale is {sa.get('scale')}, not {p['scale']}")
        if abs(sa["sweep_time_s"] - sweep_s) > 1e-6 * sweep_s:
            job.notes.append(f"the SA took sweep time {sa['sweep_time_s']:g} s (asked for {sweep_s:g} s)")
        # what the SA actually used, for the analysis (params has what was typed)
        rec["spa_setup"] = sa
        rec.update(rbw_hz=sa["rbw_hz"], vbw_hz=sa["vbw_hz"], sweep_time_s=sa["sweep_time_s"])
        job.axis["title"] = (f"{sa['center_hz'] / 1e9:g} GHz, RBW {sa['rbw_hz'] / 1e3:g} kHz, VBW {sa['vbw_hz'] / 1e3:g} kHz, "
                             f"sweep {sa['sweep_time_s']:g} s (as the SA took them)")

        st = snapshot[NOISE]
        first = points[0][1]
        job.progress(f"setting the noise source bias to {first:g} V")
        if st["output"]:
            mgr.apply(NOISE, owner, offset_v=first)
        else:
            mgr.apply(NOISE, owner, function="DC", offset_v=first)
            mgr.apply(NOISE, owner, output=True)

        traces_done, clipped = 0, []
        for i, (k, bias) in enumerate(points):
            job.check()
            job.progress(f"{bias:g} V ({i + 1}/{len(points)}" + (f", pass {k + 1}" if p["passes"] > 1 else "")
                         + f"), {p['traces']} traces", points_done=i)
            mgr.apply(NOISE, owner, offset_v=bias)
            job.sleep(p["settle_s"])
            t0 = time.time()
            samples = []
            for _ in range(p["traces"]):
                job.check()
                with mgr.use("spa", owner) as spa:
                    tr = spa.read_trace()
                    settle = spa.settle_after_read_s
                y = np.asarray(tr.y, dtype=float)
                if str(tr.y_unit).upper() == "DBM":
                    y = dbm_to_w(y)
                samples.append(y)
                traces_done += 1
                job.progress(traces_done=traces_done)
                if settle:
                    job.sleep(settle)       # the notebooks wait 50 ms after each zero-span read
            t1 = time.time()
            s = np.array(samples)
            if s.max() > ref_w:
                clipped.append(bias)
            rec["bias_v"].append(bias)
            rec["pass"].append(k)
            rec["traces"].append(p["traces"])
            rec["point_start_time"].append(round(t0, 3))
            rec["point_end_time"].append(round(t1, 3))
            rec["power_w"].append(float(s.mean()))
            rec["power_std_w"].append(float(s.std()))
            rec["trace_mean_w"].append(s.mean(axis=1))
            rec["trace_std_w"].append(s.std(axis=1))
            if p["save_samples"]:
                rec["samples_w"].append(s)
            job.add_row({"bias_v": bias, "pass": k, "traces": p["traces"],
                         "power_w": float(s.mean()), "power_std_w": float(s.std())})
        if clipped:
            job.notes.append(f"samples above the reference level ({ref_w:.3g} W) at "
                             f"{len(clipped)} point(s), e.g. {clipped[0]:g} V: the SA may have clipped; "
                             f"raise the reference level")
        job.progress(f"{len(points)} points measured", points_done=len(points))

    def _pump_start(self, p, job):
        """Set the pump as asked (the power before the output goes on), wait, and save its state."""
        mgr, owner, rec = job.mgr, job.owner, job.record
        if p["pump_mode"] == "set":
            job.progress(f"setting the TWPA pump to {p['pump_frequency_hz'] / 1e9:g} GHz, "
                         f"{p['pump_power_dbm']:g} dBm")
            mgr.apply(SYNTH, owner, **{k: p[k] for k in PUMP_KEYS})
            mgr.apply(SYNTH, owner, **{PUMP_ON: True})
        elif p["pump_mode"] == "off":
            job.progress("turning the TWPA pump off")
            mgr.apply(SYNTH, owner, **{PUMP_ON: False})
        if p["pump_mode"] != "keep" and p["pump_settle_s"] > 0:
            job.progress(f"waiting {p['pump_settle_s']:g} s for the TWPA pump")
            job.sleep(p["pump_settle_s"])
        st = mgr.get_state(SYNTH, owner)
        rec["pump_state"] = {k: st[k] for k in (PUMP_ON, *PUMP_KEYS, "pump_locked") if k in st}
        if st.get(PUMP_ON) and st.get("pump_locked") is False:
            job.notes.append("the SynthHD pump channel is not locked")

    def _pump_restore(self, job, st0: dict, done: list[str]):
        """Back to st0: frequency and power (with the output off, if it was off), then on
        if it was on. Old values outside the limits are not sent."""
        mgr, owner = job.mgr, job.owner
        lim = self.limits(SYNTH)
        if not st0.get(PUMP_ON):
            mgr.apply(SYNTH, owner, **{PUMP_ON: False})
        old = {k: st0[k] for k in PUMP_KEYS if k in st0 and within(lim.get(k), st0[k])}
        if len(old) < len(PUMP_KEYS):
            job.notes.append("TWPA pump frequency/power not all restored: the old values are outside the limits")
        if old:
            mgr.apply(SYNTH, owner, **old)
        if st0.get(PUMP_ON):
            mgr.apply(SYNTH, owner, **{PUMP_ON: True})
        done.append("TWPA pump restored")

    def _finish(self, p, job, snapshot):
        """Leave the instruments as asked. After "outputs off" nothing is sent: the
        outputs are already off and every command would be refused. Restoring the SA
        sets its old values; RBW/VBW auto coupling is not restored."""
        mgr, owner = job.mgr, job.owner
        if mgr.halt.is_set():
            return
        sa0, ns0, sw0 = snapshot.get("spa", {}), snapshot.get(NOISE, {}), snapshot.get("warm_switches", {})
        set_flux = p["flux_mode"] == "set"
        done = []
        try:
            if p["finish"] == "restore":
                if ns0["output"]:
                    mgr.apply(NOISE, owner, offset_v=ns0["offset_v"])
                else:
                    mgr.apply(NOISE, owner, output=False)
                    if within(self.limits(NOISE).get("offset_v"), ns0["offset_v"]):
                        mgr.apply(NOISE, owner, function=ns0["function"], offset_v=ns0["offset_v"])
                    else:
                        mgr.apply(NOISE, owner, offset_v=0.0)
                        job.notes.append(f"noise source left at 0 V: the old offset {ns0['offset_v']:g} V "
                                         f"is outside the limits")
                done.append("noise source restored")
                if set_flux:
                    flux_finish(job, snapshot.get(FLUX, {}), "restore", self.limits(FLUX), done)
                restore = {k: sa0[k] for k in SA_KEYS if k in sa0}
                if sa0.get("sweep_time_auto"):
                    restore["sweep_time_s"] = None      # only outside zero span; the span is restored first
                ref = restore.pop("ref_level", None)
                mgr.apply("spa", owner, **restore)
                if ref is not None:
                    mgr.apply("spa", owner, ref_level=ref)      # in the old unit, set above
                done.append("SA restored")
                if p["set_switches"] and "error" not in sw0:
                    mgr.apply("warm_switches", owner, **{k: sw0[k] for k in SWITCH_PATH if k in sw0})
                    done.append("switches restored")
                if p["pump_mode"] != "keep" and "error" not in snapshot.get(SYNTH, {}):
                    self._pump_restore(job, snapshot[SYNTH], done)
                if snapshot.get(SYNTH, {}).get(PROBE_ON):
                    mgr.apply(SYNTH, owner, **{PROBE_ON: True})        # after the switches are back
                    done.append("SynthHD probe back on")
            elif p["finish"] == "off":
                mgr.apply(NOISE, owner, offset_v=0.0)
                mgr.apply(NOISE, owner, output=False)
                done.append("noise source at 0 V, off")
                if p["pump_mode"] == "set":
                    mgr.apply(SYNTH, owner, **{PUMP_ON: False})
                    done.append("TWPA pump off")
                if set_flux:
                    flux_finish(job, snapshot.get(FLUX, {}), "off", self.limits(FLUX), done)
            with mgr.use("spa", owner) as spa:
                spa.idle()          # continuous sweep, so the front panel works again
        except Exception as e:
            job.notes.append(f"finish step failed: {e}")
            log.error("%s: finish step failed: %s", self.id, e)
        finally:
            if done:
                job.record["finish"] = done
