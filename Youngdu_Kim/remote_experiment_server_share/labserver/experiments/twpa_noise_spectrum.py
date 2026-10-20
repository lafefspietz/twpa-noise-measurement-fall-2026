"""TWPA noise spectrum: SA spectra over a wide span with the hot wire at 0 V and at a bias,
the TWPA pump off and on, for the system noise vs frequency.

Four states, each a swept spectrum (average detector, power averaging, LOG, unit dBm):
pump off / on x hot wire 0 V / on. A run repeats them in rounds, every other round in
reverse order (off-0, off-on, on-on, on-0, then on-0, on-on, off-on, off-0, ...), so the
pump toggles once per round and slow drifts cancel between the states. Each state's
spectra are averaged in W over all rounds.

At every frequency, P = a (T_N + T_add) for each pump state, with the hot-wire noise T_N
known for both hot-wire states (the hot-wire calibration). The run keeps only what needs
no calibration:
  * Y (pump off, pump on) = P(hot wire on) / P(hot wire 0 V)
  * gain = [P(on, on) - P(on, 0)] / [P(off, on) - P(off, 0)] (the hot wire's step through
    the chain with the pump on over the same with it off; T_N cancels). With a four-wave
    TWPA the step also holds the hot wire's noise at the idler 2 f_p - f, so this reads
    above the TWPA gain; the TWPA VNA gain measures that alone.
T_add and T_sys follow in the analysis, from Y and the hot-wire T_N at each frequency.

Signal path: fridge_in -> rf_probe, rx_source -> fridge, receiver -> spa; the SMW200A probe
is turned off first (as in the Y factor measurement).
"""
from __future__ import annotations

import logging
import time

import numpy as np

from ..drivers.e7405a import N_POINTS, dbm_to_w
from .base import Experiment, Param
from .qubit_flux_sweep import FLUX, flux_finish, flux_setup, flux_start
from .sa_spectrum import SaSpectrumMixin, sa_freqs, trace_dbm
from .y_factor import (NOISE, PROBE, PROBE_ON, PUMP_KEYS, PUMP_ON, SWITCH_PATH, SYNTH, TRACE_OVERHEAD_S,
                       bias_setup, noise_restore, noise_start, pump_text, synth_restore)

log = logging.getLogger("labserver.experiments")

MAX_RBW_HZ = 100e3          # above this the average detector reads noise low (1 MHz: ~1 dB)
VBW_RATIO = 30              # VBW/RBW below this: the VBW filter averages in the vertical scale first
HEADROOM_DB = 10            # warn when a state's spectrum comes closer than this to the reference level

STATES = [("off", "0"), ("off", "on"), ("on", "on"), ("on", "0")]     # (pump, hot wire), even rounds
STATE_LABEL = {("off", "0"): "pump off, hot wire 0 V", ("off", "on"): "pump off, hot wire on",
               ("on", "0"): "pump on, hot wire 0 V", ("on", "on"): "pump on, hot wire on"}

ROUNDS = "Rounds"
SA = "Spectrum analyzer (average detector, power)"
WIRE = "Hot wire (noise source bias)"
PUMP = "TWPA pump"
QUBIT = "Qubit flux bias"
END = "Before and after"
SAVE = "Save"


def state_key(pump: str, wire: str) -> str:
    return f"pump_{pump}_wire_{wire}"


def sweep_order(rounds: int) -> list[tuple[int, str, str]]:
    """[(round, pump, hot wire)], every other round reversed."""
    return [(k, *s) for k in range(rounds) for s in (STATES if k % 2 == 0 else STATES[::-1])]


def derived(power_w: dict) -> dict:
    """Y with the pump off and on, and the gain in dB, from the state averages (None where a
    state is missing or the hot wire's step is not > 0)."""
    p = {k: np.asarray(v, dtype=float) for k, v in power_w.items()}
    out = {}
    with np.errstate(divide="ignore", invalid="ignore"):
        for pump in ("off", "on"):
            a, b = p.get(state_key(pump, "on")), p.get(state_key(pump, "0"))
            if a is not None and b is not None:
                out[f"y_pump_{pump}"] = a / b
        steps = [p.get(state_key(pump, "on")) - p.get(state_key(pump, "0"))
                 if p.get(state_key(pump, "on")) is not None and p.get(state_key(pump, "0")) is not None else None
                 for pump in ("off", "on")]
        if steps[0] is not None and steps[1] is not None:
            g = np.where((steps[0] > 0) & (steps[1] > 0), 10 * np.log10(steps[1] / steps[0]), np.nan)
            out["gain_db"] = g
    return {k: [None if not np.isfinite(x) else round(float(x), 5) for x in v] for k, v in out.items()}


class TwpaNoiseSpectrum(SaSpectrumMixin, Experiment):
    id = "twpa_noise_spectrum"
    title = "TWPA noise spectrum"
    summary = ("SA spectra over a wide span with the hot wire at 0 V and at a bias, TWPA pump off and on, "
               "repeated in rounds: the hot-wire Y and gain at every frequency, for the system noise "
               "vs frequency in the analysis. Result: the four spectra, Y and gain vs frequency.")
    instruments = ["spa", NOISE, "warm_switches", PROBE, SYNTH, FLUX]
    turns_on = "the noise source bias and the TWPA pump (and the flux bias, if set)"
    params = [
        Param("rounds", "rounds", kind="int", default=4, group=ROUNDS,
              help="Each round takes the four spectra (pump off / on x hot wire 0 V / on); every other round goes "
                   "the other way, so the pump toggles once per round and slow drifts cancel."),
        Param("traces", "spectra per state per round", kind="int", default=1, group=ROUNDS),
        Param("settle_s", "wait after each hot-wire step", unit="s", default=0.5, group=ROUNDS),
        Param("pump_settle_s", "wait after the pump goes on / off", unit="s", default=2.0, group=ROUNDS),
        # SA
        Param("center_hz", "center", unit="GHz", scale=1e9, default=6.0e9, group=SA, from_state=["spa", "center_hz"]),
        Param("span_hz", "span", unit="MHz", scale=1e6, default=1000e6, group=SA, from_state=["spa", "span_hz"],
              help="401 points across the span; the average detector averages over each point's share of it."),
        Param("rbw_hz", "RBW", unit="kHz", scale=1e3, default=100e3, group=SA, from_state=["spa", "rbw_hz"],
              help="100 kHz or less: wider, the average detector reads noise low (~1 dB at 1 MHz)."),
        Param("vbw_hz", "VBW", unit="kHz", scale=1e3, default=3e6, group=SA, from_state=["spa", "vbw_hz"],
              help="3 MHz (the maximum): the VBW filter averages in the vertical scale before the detector."),
        Param("sweep_time_s", "sweep time", unit="s", default=10.0, group=SA,
              help="Long: each point averages only ~125 independent samples per second of sweep (RBW 100 kHz)."),
        Param("ref_level_dbm", "reference level", unit="dBm", default=-60.0, group=SA,
              help=f"At least {HEADROOM_DB} dB above the highest mean level (pump and hot wire on): noise peaks "
                   f"above it are clipped, which lowers that state most."),
        # hot wire
        Param("bias_on_v", "hot wire on at", unit="V", default=0.1, group=WIRE,
              help="33220A DC offset for the 'on' state; 'off' is 0 V with the output on (as in the hot-wire "
                   "calibration). Limit: noise_source_bias DC offset in the settings tab."),
        # pump
        Param("pump_frequency_hz", "pump frequency", unit="GHz", scale=1e9, default=8.05e9, group=PUMP,
              from_state=[SYNTH, "pump_frequency_hz"], help="Limit: rf_synth pump frequency in the settings tab."),
        Param("pump_power_dbm", "pump power", unit="dBm", default=7.0, group=PUMP,
              from_state=[SYNTH, "pump_power_dbm"], help="Limit: rf_synth pump power in the settings tab."),
        # qubit
        Param("flux_mode", "qubit flux bias", kind="select", default="set", group=QUBIT,
              options=[["set", "set it for the run"], ["keep", "leave it as it is"]],
              help="Use the same flux bias as the hot-wire calibration. Either way it is reserved and its state saved."),
        Param("flux_bias_v", "flux bias", unit="V", default=0.0, group=QUBIT, show_if={"flux_mode": ["set"]},
              from_state=[FLUX, "level"], help="Limit: qubit_flux_bias level (VOLT mode) in the settings tab."),
        # before / after
        Param("set_switches", "set the warm switches to probe -> fridge -> SA first", kind="bool", default=True,
              group=END, help="fridge_in -> rf_probe, rx_source -> fridge, receiver -> spa. The probe (SMW200A) is "
                              "turned off either way."),
        Param("finish", "when done", kind="select", default="restore", group=END,
              options=[["restore", "restore the SA, switches, probe, pump, flux bias and noise source as they were"],
                       ["last", "leave everything at the last state (the probe stays off)"],
                       ["off", "turn the noise source (at 0 V) and the pump off, and the flux bias (ramped to 0 V) "
                               "if the run set it; the SA and switches stay"]]),
        # save
        Param("fridge_temperature_k", "fridge temperature (for the record)", unit="K", default=None, group=SAVE),
        Param("label", "label", kind="text", default="twpa-noise-spectrum", group=SAVE),
        Param("note", "note", kind="text", default="", group=SAVE),
    ]

    # ---- plan ----
    def plan(self, p: dict, mgr=None) -> dict:
        errors, warnings, notes = [], [], []
        if p["rounds"] < 1 or p["traces"] < 1:
            errors.append("rounds and spectra per state must be >= 1")
        order = sweep_order(max(p["rounds"], 0))
        if len(order) * p["traces"] > self.cap("max_total_points"):
            errors.append(f"more than {self.cap('max_total_points')} spectra in all; use fewer rounds")
        if p["settle_s"] < 0 or p["pump_settle_s"] < 0:
            errors.append("waits must be >= 0")
        self.check_limit(errors, NOISE, "offset_v", [0.0, p["bias_on_v"]], "hot-wire bias", "V")
        if p["bias_on_v"] == 0:
            errors.append("hot wire on at 0 V is the same as off")
        self.check_limit(errors, SYNTH, "pump_frequency_hz", p["pump_frequency_hz"], "pump frequency", "Hz")
        self.check_limit(errors, SYNTH, "pump_power_dbm", p["pump_power_dbm"], "pump power", "dBm")
        if not 10e6 <= p["pump_frequency_hz"] <= 15e9:
            errors.append("pump frequency must be within 0.01..15 GHz (SynthHD)")
        if not -80 <= p["pump_power_dbm"] <= 20:
            errors.append("pump power must be within -80..20 dBm (SynthHD)")
        if p["flux_mode"] == "set":
            self.check_limit(errors, FLUX, "level", p["flux_bias_v"], "flux bias", "V")
        if p["span_hz"] <= 0:
            errors.append("span must be > 0 (this takes spectra, not zero span)")
        if p["rbw_hz"] <= 0 or p["vbw_hz"] <= 0:
            errors.append("RBW and VBW must be > 0")
        if p["sweep_time_s"] is None or p["sweep_time_s"] <= 0:
            errors.append("sweep time must be > 0")
        if not errors:
            lo, hi = p["center_hz"] - p["span_hz"] / 2, p["center_hz"] + p["span_hz"] / 2
            if lo <= p["pump_frequency_hz"] <= hi:
                errors.append(f"the pump ({p['pump_frequency_hz'] / 1e9:g} GHz) is inside the span; move the span")
            elif min(abs(p["pump_frequency_hz"] - lo), abs(p["pump_frequency_hz"] - hi)) < 50e6:
                warnings.append("the pump is within 50 MHz of the span: its skirt may reach the edge points")
            if p["rbw_hz"] > MAX_RBW_HZ:
                warnings.append(f"RBW {p['rbw_hz'] / 1e3:g} kHz: the average detector reads noise low (~1 dB at "
                                f"1 MHz); use RBW <= 100 kHz")
            if p["vbw_hz"] < VBW_RATIO * p["rbw_hz"]:
                warnings.append(f"VBW {p['vbw_hz'] / 1e3:g} kHz: the VBW filter averages in the vertical scale "
                                f"before the detector, so the noise reads low; use VBW >= {VBW_RATIO} x RBW")
        for key, value, what, unit in (("center_hz", p["center_hz"], "SA center", "Hz"),
                                       ("span_hz", p["span_hz"], "SA span", "Hz"),
                                       ("rbw_hz", p["rbw_hz"], "RBW", "Hz"), ("vbw_hz", p["vbw_hz"], "VBW", "Hz"),
                                       ("sweep_time_s", p["sweep_time_s"], "sweep time", "s"),
                                       ("ref_level_dbm", p["ref_level_dbm"], "SA reference level", "dBm")):
            if value is not None:
                self.check_limit(errors, "spa", key, value, what, unit)

        sa_avg = 1
        if mgr is not None:
            sa_avg = self.check_instruments(mgr, p, errors, warnings, notes)
        n = len(order) * p["traces"]
        sweep_s = p["sweep_time_s"] or 0
        estimate_s = (n * (sweep_s * sa_avg + TRACE_OVERHEAD_S) + len(order) * max(p["settle_s"], 0)
                      + (p["rounds"] + 1) * max(p["pump_settle_s"], 0))
        size_mb = n * N_POINTS * 26 / 1e6
        if order and not errors:
            per_point = 1 / np.sqrt(125 * sweep_s * p["traces"] * p["rounds"]) if sweep_s else 0
            notes.append(f"{p['rounds']} rounds x 4 states x {p['traces']} = {n} spectra of {sweep_s:g} s; "
                         f"each state ~{per_point * 100:.1f} % per point (power), before any smoothing")
        return {"points": [{"i": i, "round": k + 1, "pump": pump, "hot_wire_v": p["bias_on_v"] if w == "on" else 0.0,
                            "traces": p["traces"]} for i, (k, pump, w) in enumerate(order)],
                "estimate_s": round(estimate_s, 1), "size_mb": round(size_mb, 2),
                "columns": [["i", "#", ""], ["round", "round", ""], ["pump", "pump", ""],
                            ["hot_wire_v", "hot wire", "V"], ["traces", "spectra", ""]],
                "errors": errors, "warnings": warnings, "notes": notes}

    def check_instruments(self, mgr, p, errors, warnings, notes) -> int:
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
                warnings.append(f"the SA's own averaging is on: each spectrum is {sa_avg} sweeps")
            if sa.get("attenuation_db"):
                notes.append(f"SA input attenuation is {sa['attenuation_db']:g} dB (not changed)")
        except Exception as e:
            warnings.append(f"could not read the SA: {e}")
        try:
            err, note = bias_setup(self.read_state(mgr, NOISE), 0.0)
            (errors if err else notes).append(err or note)
        except Exception as e:
            errors.append(f"could not read the noise source bias: {e}")
        try:
            synth = self.read_state(mgr, SYNTH)
            notes.append(f"TWPA pump is {pump_text(synth)}: set to {p['pump_frequency_hz'] / 1e9:g} GHz, "
                         f"{p['pump_power_dbm']:g} dBm and toggled each round"
                         + (", back as it was after" if p["finish"] == "restore" else ""))
        except Exception as e:
            errors.append(f"could not read the SynthHD: {e}")
        try:
            if self.read_state(mgr, PROBE).get(PROBE_ON):
                notes.append("the SMW200A probe is on: turned off first"
                             + (", back on after" if p["finish"] == "restore" else ""))
        except Exception as e:
            errors.append(f"could not read the probe (SMW200A): {e}")
        try:
            fl = self.read_state(mgr, FLUX)
            if p["flux_mode"] == "set":
                _, err, note = flux_setup(fl, [p["flux_bias_v"]])
                (errors if err else notes).append(err or note)
            else:
                notes.append("flux bias left as it is: " + (f"on at {fl['level']:g} V" if fl["output"] else "output off"))
        except Exception as e:
            (errors if p["flux_mode"] == "set" else warnings).append(f"could not read the flux bias: {e}")
        try:
            sw = self.read_state(mgr, "warm_switches")
            wrong = {k: sw.get(k) for k, v in SWITCH_PATH.items() if sw.get(k) != v}
            if wrong:
                (notes if p["set_switches"] else warnings).append(
                    ("will set warm switches: " if p["set_switches"] else "warm switches are not on the fridge -> SA "
                     "path: ") + ", ".join(f"{k} {wrong[k]} -> {SWITCH_PATH[k]}" for k in wrong))
        except Exception as e:
            warnings.append(f"could not read the warm switches: {e}")
        return sa_avg

    # ---- result ----
    @staticmethod
    def spectra_axis(rec: dict) -> dict:
        ax = {"plot": "spectra"}
        sa = rec.get("spa_setup")
        if sa and "frequency_hz" in rec:
            ax.update(f=np.asarray(rec["frequency_hz"], dtype=float).tolist(),
                      title=(f"RBW {sa['rbw_hz'] / 1e3:g} kHz, VBW {sa['vbw_hz'] / 1e3:g} kHz, sweep "
                             f"{sa['sweep_time_s']:g} s, {sa['detector']}/{sa['average_type']}; pump "
                             f"{rec['pump_frequency_hz'] / 1e9:g} GHz {rec['pump_power_dbm']:g} dBm, hot wire on "
                             f"{rec['hot_wire_on_v']:g} V"),
                      spectra={STATE_LABEL[s]: trace_dbm(rec["power_w"][state_key(*s)])
                               for s in STATES if state_key(*s) in rec.get("power_w", {})},
                      derived={k: rec[k] for k in ("y_pump_off", "y_pump_on", "gain_db") if k in rec})
        return ax

    @staticmethod
    def spectra_row(rec: dict, i: int) -> dict:
        return {k: rec[k][i] for k in ("round", "pump", "hot_wire_v", "traces", "mean_dbm", "max_dbm")}

    def result_view(self, rec: dict) -> dict:
        return {"axis": self.spectra_axis(rec), "rows": [self.spectra_row(rec, i) for i in range(len(rec["round"]))]}

    # ---- run ----
    def run(self, p: dict, job) -> None:
        plan = self.plan(p)
        if plan["errors"]:
            raise ValueError("; ".join(plan["errors"]))
        order = sweep_order(p["rounds"])
        mgr, owner = job.mgr, job.owner
        job.progress("reserving instruments", points_total=len(order), traces_total=len(order) * p["traces"])
        with mgr.claim(self.instruments, owner):
            snapshot = mgr.snapshot(owner=owner)
            for name in ("spa", NOISE, PROBE, SYNTH) + ((FLUX,) if p["flux_mode"] == "set" else ()):
                if "error" in snapshot.get(name, {}):
                    raise RuntimeError(f"cannot read {name}: {snapshot[name]['error']}")
            err, _ = bias_setup(snapshot[NOISE], 0.0)
            if err:
                raise RuntimeError(err)
            rec = job.record
            rec.update(kind="experiment", experiment=self.id, params=self.recorded_params(p), note=p["note"],
                       sim=mgr.sim, fridge_temperature_k=p["fridge_temperature_k"], hot_wire_on_v=p["bias_on_v"],
                       pump_frequency_hz=p["pump_frequency_hz"], pump_power_dbm=p["pump_power_dbm"],
                       round=[], pump=[], hot_wire_v=[], traces=[], sweep_start_time=[], sweep_end_time=[],
                       mean_dbm=[], max_dbm=[], traces_w=[], power_w={}, instruments=snapshot)
            job.axis = self.spectra_axis(rec)
            try:
                self._measure(p, job, order, snapshot)
            finally:
                self._finish(p, job, snapshot)

    def _measure(self, p, job, order, snapshot):
        mgr, owner, rec = job.mgr, job.owner, job.record
        job.progress("turning the probe off")
        mgr.apply(PROBE, owner, **{PROBE_ON: False})
        if p["set_switches"]:
            job.progress("setting the warm switches")
            mgr.apply("warm_switches", owner, **SWITCH_PATH)
        if p["flux_mode"] == "set":
            flux_start(job, snapshot[FLUX], [p["flux_bias_v"]])
        try:
            rec["flux_bias_state"] = mgr.get_state(FLUX, owner)
        except Exception as e:
            job.notes.append(f"could not read the flux bias: {e}")

        sa = self._setup_sa(p, job, p["center_hz"], detector="AVER")
        freqs = sa_freqs(sa["center_hz"], sa["span_hz"])
        rec["frequency_hz"] = freqs
        job.axis = self.spectra_axis(rec)

        job.progress("setting the TWPA pump (off) and the hot wire (0 V)")
        mgr.apply(SYNTH, owner, **{PUMP_ON: False})
        mgr.apply(SYNTH, owner, **{k: p[k] for k in PUMP_KEYS})
        noise_start(job, snapshot[NOISE], 0.0)
        pump_on = False
        sums = {state_key(*s): np.zeros(len(freqs)) for s in STATES}
        counts = dict.fromkeys(sums, 0)
        close = []
        for i, (k, pump, wire) in enumerate(order):
            job.check()
            want_on = pump == "on"
            if want_on != pump_on:
                job.progress(f"turning the TWPA pump {pump}")
                mgr.apply(SYNTH, owner, **{PUMP_ON: want_on})
                pump_on = want_on
                job.sleep(p["pump_settle_s"])
                if want_on and "pump_state" not in rec:
                    st = mgr.get_state(SYNTH, owner)
                    rec["pump_state"] = {key: st[key] for key in (PUMP_ON, *PUMP_KEYS, "pump_locked") if key in st}
                    if st.get("pump_locked") is False:
                        job.notes.append("the SynthHD pump channel is not locked")
            bias = p["bias_on_v"] if wire == "on" else 0.0
            mgr.apply(NOISE, owner, offset_v=bias)
            job.sleep(p["settle_s"])
            job.progress(f"round {k + 1}/{p['rounds']}: {STATE_LABEL[(pump, wire)]}", points_done=i)
            t0 = time.time()
            p_w = self._spectrum(p, job, freqs, p["traces"])
            t1 = time.time()
            key = state_key(pump, wire)
            sums[key] += p_w
            counts[key] += 1
            rec["power_w"][key] = sums[key] / counts[key]
            for name, value in (("round", k + 1), ("pump", pump), ("hot_wire_v", bias), ("traces", p["traces"]),
                                ("sweep_start_time", round(t0, 3)), ("sweep_end_time", round(t1, 3)),
                                ("mean_dbm", round(10 * np.log10(p_w.mean() / 1e-3), 4)),
                                ("max_dbm", round(10 * np.log10(p_w.max() / 1e-3), 4))):
                rec[name].append(value)
            rec["traces_w"].append(p_w)
            if p_w.max() > dbm_to_w(p["ref_level_dbm"] - HEADROOM_DB) and key not in close:
                close.append(key)
            rec.update(derived(rec["power_w"]))
            job.axis = self.spectra_axis(rec)
            job.add_row(self.spectra_row(rec, i))
        if close:
            job.notes.append(f"{', '.join(STATE_LABEL[s] for s in STATES if state_key(*s) in close)}: less than "
                             f"{HEADROOM_DB} dB below the reference level ({p['ref_level_dbm']:g} dBm); noise peaks "
                             f"may have been clipped, raise the reference level")
        job.progress(f"{len(order)} spectra measured", points_done=len(order))

    def _finish(self, p, job, snapshot):
        """Leave the instruments as asked. After "outputs off" nothing is sent."""
        mgr, owner = job.mgr, job.owner
        if mgr.halt.is_set():
            return
        sa0, sw0, ns0 = snapshot.get("spa", {}), snapshot.get("warm_switches", {}), snapshot.get(NOISE, {})
        set_flux = p["flux_mode"] == "set"
        done = []
        try:
            if p["finish"] == "restore":
                noise_restore(self, job, ns0, done)
                if set_flux:
                    flux_finish(job, snapshot.get(FLUX, {}), "restore", self.limits(FLUX), done)
                self._restore_sa(job, sa0, done)
                if p["set_switches"] and "error" not in sw0:
                    mgr.apply("warm_switches", owner, **{k: sw0[k] for k in SWITCH_PATH if k in sw0})
                    done.append("switches restored")
                if "error" not in snapshot.get(SYNTH, {}):
                    synth_restore(self, job, snapshot[SYNTH], done)
                if snapshot.get(PROBE, {}).get(PROBE_ON):
                    mgr.apply(PROBE, owner, **{PROBE_ON: True})        # after the switches are back
                    done.append("probe back on")
            elif p["finish"] == "off":
                mgr.apply(NOISE, owner, offset_v=0.0)
                mgr.apply(NOISE, owner, output=False)
                mgr.apply(SYNTH, owner, **{PUMP_ON: False})
                done.append("noise source at 0 V, off; TWPA pump off")
                if set_flux:
                    flux_finish(job, snapshot.get(FLUX, {}), "off", self.limits(FLUX), done)
            with mgr.use("spa", owner) as spa:
                spa.idle()
        except Exception as e:
            job.notes.append(f"finish step failed: {e}")
            log.error("%s: finish step failed: %s", self.id, e)
        finally:
            if done:
                job.record["finish"] = done
