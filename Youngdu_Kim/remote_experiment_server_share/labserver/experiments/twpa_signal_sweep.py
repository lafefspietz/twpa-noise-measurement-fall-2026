"""TWPA signal sweep: SNR vs signal frequency (or power) at a fixed pump, optionally also vs
the noise source bias, on the SA.

The TWPA pump (SynthHD channel B) is set to one frequency and power for the whole run. The
signal (the SMW200A probe) is stepped in frequency (power fixed) or in power
(frequency fixed), and the SA, centred on the signal frequency (plus an optional fixed
offset, for SMW200A and SA clocks not locked to one 10 MHz reference) with a narrow span,
takes an averaged spectrum at each point. The noise source bias (33220A DC offset, the hot
wire, whose noise reaches the TWPA input and goes as V^2) can be stepped as the outer loop,
to see how the SNR falls as the input noise rises; or left as it is.

At each noise source bias: a pump-off pass over every signal point (the reference,
optional), then the pump on and the same points again. From each spectrum (traces averaged
in W, sample detector, LOG scale; sa_spectrum.analyze):
  * noise  = mean of the bins with inner <= |f - f_SA| <= outer (both sides)
  * signal = max of the bins within +-window of the SA center, minus the noise (raw max: peak_w)
  * SNR    = signal / noise
and against the pump-off spectrum of the same signal point and bias:
  * gain = signal / signal_off, noise rise = noise / noise_off, SNRI = SNR / SNR_off

With VBW << RBW the SA's video filter averages the log of the noise, so the noise reads
about 2.5 dB low while the tone does not: it cancels in the noise rise and SNRI, not in the
absolute SNR (LOG_BIAS_NOTE).

Signal path: fridge_in -> rf_probe, rx_source -> fridge, receiver -> spa (as the TWPA pump
sweep). Result: per point snr_db (and gain_db, noise_rise_db, snri_db with the reference),
a 2D map signal x noise source bias in the UI (a line with one bias), with the spectrum of
the point under the cursor over its pump-off spectrum.
"""
from __future__ import annotations

import logging
import time

import numpy as np

from ..drivers.base import within
from ..drivers.e7405a import N_POINTS, dbm_to_w
from ..drivers.rcdat import STEP_DB as ATT_STEP_DB
from ..drivers.smw200a import FREQ_RANGE_HZ as PROBE_FREQ_HZ, POWER_RANGE_DBM as PROBE_POWER_DBM
from .base import Experiment, Param
from .qubit_flux_sweep import FLUX, flux_finish, flux_setup, flux_start
from .sa_spectrum import (LOG_BIAS_NOTE, SaSpectrumMixin, analyze, check_center_offset, check_centered_regions,
                          estimate_sweep_s, ratio_db, sa_freqs, trace_dbm, w_to_dbm)
from .vna_sweep import sweep_values
from .y_factor import (NOISE, PROBE, PROBE_ON, PUMP_ON, SWITCH_PATH, SYNTH, TRACE_OVERHEAD_S, bias_setup, noise_restore,
                       noise_start, pump_text, synth_restore)

log = logging.getLogger("labserver.experiments")

ATT = "attenuator"
STEP_OVERHEAD_S = 0.15      # probe frequency + SA center (each apply reads the state back), rough
ON_KEYS = ("signal_w", "peak_w", "peak_hz", "noise_w", "noise_std_w")
QTY_REF = ["snr_db", "gain_db", "snri_db", "noise_rise_db", "signal_dbm", "noise_dbm"]
QTY_NO_REF = ["snr_db", "signal_dbm", "noise_dbm"]

SIGNAL = "Signal (SMW200A)"
PUMP = "TWPA pump (fixed)"
NOISE_G = "Noise source bias (33220A DC, hot wire)"
SA = "Spectrum analyzer (sample detector, LOG, centred on the signal)"
ANALYSIS = "Signal and noise"
QUBIT = "Qubit flux bias"
END = "Before and after"
SAVE = "Save"


def steps(what: str, start: float, stop: float, step: float, unit: str, scale: float, max_points: int
          ) -> tuple[list[float], list[str], list[str]]:
    """start -> stop in step (start = stop: one value), plus (errors, warnings)."""
    if start == stop:
        return [start], [], []
    if step <= 0:
        return [], [f"{what} step must be > 0"], []
    if abs(stop - start) / step + 1 > max_points:
        return [], [f"more than {max_points} {what} points; use a larger step"], []
    values, warn = sweep_values(start / scale, stop / scale, step / scale, unit)
    return [round(v * scale, 6) for v in values], [], [f"{what}: {warn}"] if warn else []


def kept_bias(state: dict) -> float | None:
    """The hot-wire bias a noise source left as it is applies: its DC offset, 0 with the output
    off (or not in DC), None if it could not be read."""
    if not state or "error" in state:
        return None
    on_dc = state.get("output") and str(state.get("function", "")).upper().startswith("DC")
    return float(state["offset_v"]) if on_dc else 0.0


class TwpaSignalSweep(SaSpectrumMixin, Experiment):
    id = "twpa_signal_sweep"
    title = "TWPA signal sweep (SNR)"
    summary = ("Keep the TWPA pump fixed and step the signal (SMW200A probe) in frequency or power, reading the "
               "SNR from an SA spectrum centred on it, with a pump-off pass for gain and SNR improvement; "
               "optionally step the noise source bias as well. Result: 2D map, signal x noise source bias.")
    instruments = ["spa", PROBE, SYNTH, "warm_switches", ATT, NOISE, FLUX]
    turns_on = "the SMW200A signal and the TWPA pump (and the noise source and the flux bias, if set for the sweep)"
    params = [
        Param("signal_axis", "step the signal", kind="select", default="frequency", group=SIGNAL,
              options=[["frequency", "frequency (power fixed)"], ["power", "power (frequency fixed)"]],
              help="The inner loop: every signal point is measured at each noise source bias."),
        Param("signal_freq_start_hz", "signal frequency from", unit="GHz", scale=1e9, default=5.5e9, group=SIGNAL,
              show_if={"signal_axis": ["frequency"]}),
        Param("signal_freq_stop_hz", "to", unit="GHz", scale=1e9, default=6.5e9, group=SIGNAL,
              show_if={"signal_axis": ["frequency"]}),
        Param("signal_freq_step_hz", "step", unit="MHz", scale=1e6, default=100e6, group=SIGNAL,
              show_if={"signal_axis": ["frequency"]},
              help="From = to gives one frequency. The SA center follows the signal. Limit: rf_probe probe "
                   "frequency in the settings tab."),
        Param("signal_power_dbm", "signal power", unit="dBm", default=-20.0, group=SIGNAL,
              show_if={"signal_axis": ["frequency"]}, from_state=[PROBE, "probe_power_dbm"],
              help="At the SMW200A output. Limit: rf_probe probe power in the settings tab."),
        Param("signal_power_start_dbm", "signal power from", unit="dBm", default=-40.0, group=SIGNAL,
              show_if={"signal_axis": ["power"]}),
        Param("signal_power_stop_dbm", "to", unit="dBm", default=-10.0, group=SIGNAL,
              show_if={"signal_axis": ["power"]}),
        Param("signal_power_step_db", "step", unit="dB", default=2.0, group=SIGNAL,
              show_if={"signal_axis": ["power"]},
              help="At the SMW200A output. From = to gives one power. Limit: rf_probe probe power in the settings tab."),
        Param("signal_frequency_hz", "signal frequency", unit="GHz", scale=1e9, default=6.0e9, group=SIGNAL,
              show_if={"signal_axis": ["power"]}, from_state=[PROBE, "probe_frequency_hz"],
              help="Limit: rf_probe probe frequency in the settings tab."),
        Param("traces", "traces per point", kind="int", default=1, group=SIGNAL,
              help="SA spectra averaged (in W) at each point, pump on and pump off."),
        Param("settle_s", "wait after each signal step", unit="s", default=0.05, group=SIGNAL,
              help="Also after the signal is first turned on."),
        Param("att_mode", "warm step attenuator", kind="select", default="keep", group=SIGNAL,
              options=[["keep", "leave it as it is"], ["set", "set it for the sweep"]],
              help="RCDAT between fridge_in and the fridge, in the signal path."),
        Param("attenuation_db", "attenuation", unit="dB", default=25.0, group=SIGNAL,
              show_if={"att_mode": ["set"]}, from_state=[ATT, "attenuation_db"],
              help="Multiples of 0.25 dB. Limit: attenuator attenuation in the settings tab."),
        # pump
        Param("pump_frequency_hz", "pump frequency", unit="GHz", scale=1e9, default=8.5e9, group=PUMP,
              from_state=[SYNTH, "pump_frequency_hz"],
              help="Fixed for the whole run. Limit: rf_synth pump frequency in the settings tab."),
        Param("pump_power_dbm", "pump power", unit="dBm", default=7.0, group=PUMP,
              from_state=[SYNTH, "pump_power_dbm"],
              help="Set before the output goes on. Limit: rf_synth pump power in the settings tab."),
        Param("reference", "pump-off reference", kind="select", default="pump_off", group=PUMP,
              options=[["pump_off", "a pump-off pass at each noise source bias (gain, noise rise, SNRI)"],
                       ["none", "none: pump on only (SNR)"]],
              help="Every signal point is measured with the pump off first, then with it on, at each bias: "
                   "twice the spectra, and the pump goes off and on once per bias."),
        Param("pump_settle_s", "wait after the pump goes off / on", unit="s", default=1.0, group=PUMP),
        # noise source
        Param("noise_mode", "noise source bias", kind="select", default="keep", group=NOISE_G,
              options=[["keep", "leave it as it is"], ["sweep", "step it (outer loop)"]],
              help="The hot wire adds noise ahead of the TWPA; its noise power goes as V^2. Either way it is "
                   "reserved during the run and its state is saved."),
        Param("noise_bias_start_v", "from", unit="V", default=0.0, group=NOISE_G, show_if={"noise_mode": ["sweep"]}),
        Param("noise_bias_stop_v", "to", unit="V", default=0.2, group=NOISE_G, show_if={"noise_mode": ["sweep"]}),
        Param("noise_bias_step_v", "step", unit="V", default=0.05, group=NOISE_G, show_if={"noise_mode": ["sweep"]},
              help="33220A DC offset. Outer loop: every signal point (pump off and on) at each bias. "
                   "+-V give the same noise. Limit: noise_source_bias DC offset in the settings tab."),
        Param("noise_settle_s", "wait after each bias step", unit="s", default=0.5, group=NOISE_G,
              show_if={"noise_mode": ["sweep"]}, help="Lets the hot wire settle."),
        # SA
        Param("sa_offset_hz", "SA center - signal frequency", unit="kHz", scale=1e3, default=0.0, group=SA,
              help="The SA center at every point is the signal frequency plus this. Without a shared 10 MHz "
                   "reference the tone shows up a little off the signal frequency on the SA: set this to where "
                   "it shows up, and the signal window and the noise region follow. The clock error is in ppm, "
                   "so keep the signal window wide enough for what changes across the sweep."),
        Param("span_hz", "span", unit="MHz", scale=1e6, default=5e6, group=SA,
              help="Centred on the SA center at every point; 401 bins across it."),
        Param("rbw_hz", "RBW", unit="kHz", scale=1e3, default=30e3, group=SA, from_state=["spa", "rbw_hz"],
              help="The ESA takes 1-3-10 steps (RBW and VBW): the run reads back the values it took and saves them."),
        Param("vbw_hz", "VBW", unit="kHz", scale=1e3, default=1e3, group=SA, from_state=["spa", "vbw_hz"]),
        Param("sweep_time_s", "sweep time", unit="s", default=None, group=SA,
              help="Empty = the SA's auto sweep time (the plan estimate is rough; the run saves the real one)."),
        Param("ref_level_dbm", "reference level", unit="dBm", default=-40.0, group=SA,
              help="Keep the tone below it with the pump on, or the SA clips."),
        Param("save_traces", "save the averaged spectrum of every point", kind="bool", default=True, group=SA,
              help="Pump on and pump off: 2 x 401 numbers per point."),
        # analysis
        Param("signal_window_hz", "signal window (+-)", unit="kHz", scale=1e3, default=None, group=ANALYSIS,
              help="Signal = max of the bins within +- this of the SA center, minus the noise. "
                   "Empty = the RBW the SA takes."),
        Param("noise_inner_hz", "noise from", unit="MHz", scale=1e6, default=1e6, group=ANALYSIS,
              help="Noise = mean (in W) of the bins at least this far from the SA center, on both sides."),
        Param("noise_outer_hz", "noise to", unit="MHz", scale=1e6, default=None, group=ANALYSIS,
              help="... and at most this far. Empty = to the edge of the span."),
        # qubit
        Param("flux_mode", "qubit flux bias", kind="select", default="keep", group=QUBIT,
              options=[["keep", "leave it as it is"], ["set", "set it for the sweep"]],
              help="Keep the qubit away from the signal frequencies. Either way it is reserved during the "
                   "sweep and its state is saved."),
        Param("flux_bias_v", "flux bias", unit="V", default=0.0, group=QUBIT, show_if={"flux_mode": ["set"]},
              from_state=[FLUX, "level"],
              help="Limit: qubit_flux_bias level (VOLT mode) in the settings tab."),
        # before / after
        Param("set_switches", "set the warm switches to SMW200A probe -> fridge -> SA first", kind="bool",
              default=True, group=END,
              help="fridge_in -> rf_probe, rx_source -> fridge, receiver -> spa."),
        Param("finish", "when done", kind="select", default="restore", group=END,
              options=[["restore", "restore the SA, switches, attenuator, signal, pump, noise source and flux "
                                   "bias as they were"],
                       ["last", "leave everything at the last point"],
                       ["off", "turn the signal and the pump off (and the noise source at 0 V and the flux "
                               "bias, ramped to 0 V, if the sweep set them); the SA, switches and attenuator "
                               "stay"]]),
        # save
        Param("fridge_temperature_k", "fridge temperature (for the record)", unit="K", default=None, group=SAVE),
        Param("label", "label", kind="text", default="twpa-signal-sweep", group=SAVE),
        Param("note", "note", kind="text", default="", group=SAVE),
    ]

    # ---- settings ----
    @staticmethod
    def by_freq(p: dict) -> bool:
        return p["signal_axis"] == "frequency"

    @staticmethod
    def sweeps_noise(p: dict) -> bool:
        return p["noise_mode"] == "sweep"

    @staticmethod
    def with_ref(p: dict) -> bool:
        return p["reference"] == "pump_off"

    @staticmethod
    def noise_outer(p: dict) -> float:
        return p["noise_outer_hz"] if p["noise_outer_hz"] is not None else p["span_hz"] / 2

    @staticmethod
    def window(p: dict, rbw_hz: float | None = None) -> float:
        return p["signal_window_hz"] if p["signal_window_hz"] is not None else (rbw_hz or p["rbw_hz"])

    # ---- plan ----
    def signal_points(self, p: dict) -> tuple[list[tuple[float, float]], list[str], list[str]]:
        """[(signal frequency, signal power)] in sweep order, plus (errors, warnings)."""
        if self.by_freq(p):
            fs, errors, warnings = steps("signal frequency", p["signal_freq_start_hz"], p["signal_freq_stop_hz"],
                                         p["signal_freq_step_hz"], "MHz", 1e6, self.cap("max_points"))
            return [(f, p["signal_power_dbm"]) for f in fs], errors, warnings
        pws, errors, warnings = steps("signal power", p["signal_power_start_dbm"], p["signal_power_stop_dbm"],
                                      p["signal_power_step_db"], "dBm", 1, self.cap("max_points"))
        return [(p["signal_frequency_hz"], pw) for pw in pws], errors, warnings

    def biases(self, p: dict) -> tuple[list[float | None], list[str], list[str]]:
        """The noise source biases (outer loop); [None] = left as it is."""
        if not self.sweeps_noise(p):
            return [None], [], []
        return steps("noise source bias", p["noise_bias_start_v"], p["noise_bias_stop_v"], p["noise_bias_step_v"],
                     "V", 1, self.cap("max_points"))

    def plan(self, p: dict, mgr=None) -> dict:
        sig, errors, warnings = self.signal_points(p)
        biases, berr, bwarn = self.biases(p)
        errors, warnings, notes = errors + berr, warnings + bwarn, []
        if p["traces"] < 1:
            errors.append("traces per point must be >= 1")
        n_total = len(sig) * len(biases)
        if n_total > self.cap("max_total_points"):
            errors.append(f"{len(biases)} noise source biases x {len(sig)} signal points = {n_total} points, more "
                          f"than {self.cap('max_total_points')}; use larger steps")
        fs = sorted({f for f, _ in sig})
        pws = sorted({pw for _, pw in sig})
        off = p["sa_offset_hz"]
        if fs:
            self.check_limit(errors, PROBE, "probe_frequency_hz", [fs[0], fs[-1]], "signal frequency", "Hz")
            self.check_limit(errors, "spa", "center_hz", [fs[0] + off, fs[-1] + off], "SA frequency", "Hz")
            if not all(PROBE_FREQ_HZ[0] <= f <= PROBE_FREQ_HZ[1] for f in (fs[0], fs[-1])):
                errors.append("signal frequency must be within 0.0001..44 GHz (SMW200A)")
            check_center_offset(warnings, off, fs[-1], "signal frequency")
            near = [f for f in fs if abs(p["pump_frequency_hz"] - (f + off)) <= p["span_hz"] / 2]
            if near:
                warnings.append(f"the pump ({p['pump_frequency_hz'] / 1e9:g} GHz) is inside the SA span at signal "
                                f"{near[0] / 1e9:g} GHz" + (f" and {len(near) - 1} more" if len(near) > 1 else "")
                                + ": it (and its idler) would land in the spectrum")
        if pws:
            self.check_limit(errors, PROBE, "probe_power_dbm", [pws[0], pws[-1]], "signal power", "dBm")
            if not all(PROBE_POWER_DBM[0] <= pw <= PROBE_POWER_DBM[1] for pw in (pws[0], pws[-1])):
                errors.append("signal power must be within -145..30 dBm (SMW200A)")
        self.check_limit(errors, SYNTH, "pump_frequency_hz", p["pump_frequency_hz"], "pump frequency", "Hz")
        self.check_limit(errors, SYNTH, "pump_power_dbm", p["pump_power_dbm"], "pump power", "dBm")
        if not 10e6 <= p["pump_frequency_hz"] <= 15e9:
            errors.append("pump frequency must be within 0.01..15 GHz (SynthHD)")
        if not -80 <= p["pump_power_dbm"] <= 20:
            errors.append("pump power must be within -80..20 dBm (SynthHD)")
        if self.sweeps_noise(p) and biases:
            self.check_limit(errors, NOISE, "offset_v", biases, "noise source bias", "V")
        if p["att_mode"] == "set":
            self.check_limit(errors, ATT, "attenuation_db", p["attenuation_db"], "attenuation", "dB")
            if abs(p["attenuation_db"] / ATT_STEP_DB - round(p["attenuation_db"] / ATT_STEP_DB)) > 1e-9:
                errors.append(f"attenuation not a multiple of {ATT_STEP_DB:g} dB")
        if p["flux_mode"] == "set":
            self.check_limit(errors, FLUX, "level", p["flux_bias_v"], "flux bias", "V")
        if p["rbw_hz"] <= 0 or p["vbw_hz"] <= 0:
            errors.append("RBW and VBW must be > 0")
        else:
            check_centered_regions(errors, warnings, p["span_hz"], p["noise_inner_hz"], self.noise_outer(p),
                                   self.window(p), p["rbw_hz"], what="signal window")
        if p["sweep_time_s"] is not None and p["sweep_time_s"] <= 0:
            errors.append("sweep time must be > 0; leave it empty for the SA's auto sweep time")
        if min(p["settle_s"], p["pump_settle_s"], p["noise_settle_s"]) < 0:
            errors.append("waits must be >= 0")
        for key, value, what, unit in (("span_hz", p["span_hz"], "SA span", "Hz"),
                                       ("rbw_hz", p["rbw_hz"], "RBW", "Hz"), ("vbw_hz", p["vbw_hz"], "VBW", "Hz"),
                                       ("sweep_time_s", p["sweep_time_s"], "sweep time", "s"),
                                       ("ref_level_dbm", p["ref_level_dbm"], "SA reference level", "dBm")):
            if value is not None:
                self.check_limit(errors, "spa", key, value, what, unit)

        sa_avg = 1
        if mgr is not None:
            sa_avg = self.check_instruments(mgr, p, biases, errors, warnings, notes)
        sweep_s = p["sweep_time_s"]
        if sweep_s is None and p["rbw_hz"] > 0 and p["vbw_hz"] > 0 and p["span_hz"] > 0:
            sweep_s = estimate_sweep_s(p["span_hz"], p["rbw_hz"], p["vbw_hz"])
            notes.append(f"sweep time about {sweep_s:.2g} s per spectrum (rough; SA auto)")
        passes = 2 if self.with_ref(p) else 1
        spectra = n_total * passes * max(p["traces"], 0)
        step_s = max(p["settle_s"], 0) + (STEP_OVERHEAD_S if len(fs) > 1 else 0)
        estimate_s = (spectra * ((sweep_s or 0) * sa_avg + TRACE_OVERHEAD_S) + n_total * passes * step_s
                      + (2 * len(biases) if self.with_ref(p) else 1) * max(p["pump_settle_s"], 0)
                      + (len(biases) * max(p["noise_settle_s"], 0) if self.sweeps_noise(p) else 0))
        size_mb = n_total * (30 + (passes * N_POINTS if p["save_traces"] else 0)) * 26 / 1e6
        if size_mb > self.cap("big_file_mb"):
            warnings.append(f"the data file would be about {size_mb:.0f} MB; turn off saving every spectrum")
        if sig and biases:
            what = (f"{len(fs)} signal frequencies ({fs[0] / 1e9:g} -> {fs[-1] / 1e9:g} GHz) at {pws[0]:g} dBm"
                    if self.by_freq(p) else
                    f"{len(pws)} signal powers ({sig[0][1]:g} -> {sig[-1][1]:g} dBm) at {fs[0] / 1e9:g} GHz")
            outer = (f"{len(biases)} noise source biases ({biases[0]:g} -> {biases[-1]:g} V) x "
                     if self.sweeps_noise(p) else "")
            notes.append(f"{outer}{what} = {n_total} points, "
                         + ("pump off then on at each bias" if self.with_ref(p) and self.sweeps_noise(p)
                            else "pump off then on" if self.with_ref(p) else "pump on only")
                         + f": {spectra} spectra in all")
            if off:
                notes.append(f"SA center = signal frequency {off / 1e3:+g} kHz")
        cols = [["i", "#", ""]] + ([["noise_bias_v", "noise source bias", "V"]] if self.sweeps_noise(p) else []) \
            + [["signal_frequency_ghz", "signal frequency", "GHz"], ["signal_power_dbm", "signal power", "dBm"],
               ["traces", "traces", ""]]
        points = [{"i": i, **({"noise_bias_v": b} if self.sweeps_noise(p) else {}),
                   "signal_frequency_ghz": round(f / 1e9, 9), "signal_power_dbm": pw, "traces": p["traces"]}
                  for i, (b, (f, pw)) in enumerate((b, s) for b in biases for s in sig)]
        return {"points": points, "estimate_s": round(estimate_s, 1), "size_mb": round(size_mb, 2),
                "columns": cols, "errors": errors, "warnings": warnings, "notes": notes}

    def check_instruments(self, mgr, p, biases, errors, warnings, notes) -> int:
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

        back = ", back as it was after" if p["finish"] == "restore" else ""
        try:
            probe = self.read_state(mgr, PROBE)
            now = (f"on at {probe['probe_frequency_hz'] / 1e9:g} GHz, {probe['probe_power_dbm']:g} dBm"
                   if probe.get(PROBE_ON) else "off")
            notes.append(f"SMW200A signal (probe) is {now}: stepped over the sweep and on{back}")
        except Exception as e:
            errors.append(f"could not read the probe (SMW200A): {e}")
        try:
            synth = self.read_state(mgr, SYNTH)
            want = pump_text({PUMP_ON: True, "pump_frequency_hz": p["pump_frequency_hz"],
                              "pump_power_dbm": p["pump_power_dbm"]})
            notes.append(f"TWPA pump is {pump_text(synth)}: {want}"
                         + (", off for each pump-off pass" if self.with_ref(p) else "") + back)
        except Exception as e:
            errors.append(f"could not read the SynthHD: {e}")

        try:
            ns = self.read_state(mgr, NOISE)
            if self.sweeps_noise(p):
                if biases:
                    err, note = bias_setup(ns, biases[0])
                    (errors if err else notes).append(err or note)
            else:
                func = str(ns.get("function", "")).upper()
                if ns["output"] and not func.startswith("DC"):
                    warnings.append(f"the noise source output is on in {func or '?'} mode, not a DC bias")
                notes.append("noise source left as it is: "
                             + (f"on at {ns['offset_v']:g} V" if ns["output"] else "output off (no hot-wire noise)"))
        except Exception as e:
            (errors if self.sweeps_noise(p) else warnings).append(f"could not read the noise source bias: {e}")

        try:
            att = self.read_state(mgr, ATT)["attenuation_db"]
            notes.append(f"attenuator is at {att:g} dB" + (f": set to {p['attenuation_db']:g} dB"
                                                            if p["att_mode"] == "set" else " (not changed)"))
        except Exception as e:
            (errors if p["att_mode"] == "set" else warnings).append(f"could not read the attenuator: {e}")

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
            if not wrong:
                notes.append("warm switches are on the probe -> fridge -> SA path")
            elif p["set_switches"]:
                notes.append("will set warm switches: " + ", ".join(f"{k} {wrong[k]} -> {SWITCH_PATH[k]}"
                                                                    for k in wrong))
            else:
                warnings.append("warm switches are not on the probe -> fridge -> SA path: "
                                + ", ".join(f"{k}={v}" for k, v in wrong.items()))
        except Exception as e:
            warnings.append(f"could not read the warm switches: {e}")
        return sa_avg

    # ---- result ----
    def grid_axis(self, rec: dict) -> dict:
        """Plot axis for the record so far: signal x noise source bias, then the SA bins and
        regions once the SA is set up. Each row carries its own pump-off spectrum and SA center."""
        p = self.record_settings(rec)
        freq, ref = self.by_freq(p), self.with_ref(p)
        ax = {"plot": "grid", "x_key": "signal_frequency_hz" if freq else "signal_power_dbm",
              "x_label": "signal frequency" if freq else "signal power", "x_unit": "GHz" if freq else "dBm",
              "x_scale": 1e9 if freq else 1, "y_key": "noise_bias_v", "y_label": "noise source bias", "y_unit": "V",
              "per_point_ref": True, "quantities": QTY_REF if ref else QTY_NO_REF}
        sa = rec.get("spa_setup")
        if sa and "sa_bin_offset_hz" in rec:
            f_sa = sa["center_hz"]          # the first point's; each row has its own (sa_center_hz)
            ax.update(f=(f_sa + np.asarray(rec["sa_bin_offset_hz"], dtype=float)).tolist(), f_sa=f_sa, f_signal=f_sa,
                      noise_inner_hz=rec["noise_inner_hz"], noise_outer_hz=rec["noise_outer_hz"],
                      window_hz=rec["signal_window_hz"],
                      title=(f"pump {rec['pump_frequency_hz'] / 1e9:g} GHz, {rec['pump_power_dbm']:g} dBm, "
                             f"span {sa['span_hz'] / 1e6:g} MHz, RBW {sa['rbw_hz'] / 1e3:g} kHz, "
                             f"VBW {sa['vbw_hz'] / 1e3:g} kHz, sweep {sa['sweep_time_s']:g} s"))
        return ax

    @staticmethod
    def grid_row(rec: dict, i: int, p_w=None, ref_w=None) -> dict:
        """Result row of point i; p_w / ref_w (its pump-on / pump-off spectra in W) add the traces."""
        row = {"i": i, **{k: rec[k][i] for k in ("signal_frequency_hz", "signal_power_dbm", "noise_bias_v", "traces",
                                                 "snr_db", "peak_hz", "sa_center_hz")},
               "signal_dbm": w_to_dbm(rec["signal_w"][i]), "noise_dbm": w_to_dbm(rec["noise_w"][i])}
        if "gain_db" in rec:
            row.update({k: rec[k][i] for k in ("gain_db", "noise_rise_db", "snri_db", "ref_snr_db")},
                       ref_signal_dbm=w_to_dbm(rec["ref_signal_w"][i]), ref_noise_dbm=w_to_dbm(rec["ref_noise_w"][i]))
        if p_w is not None:
            row["trace_dbm"] = trace_dbm(p_w)
        if ref_w is not None:
            row["ref_trace_dbm"] = trace_dbm(ref_w)
        return row

    def result_view(self, rec: dict) -> dict:
        on, off = rec.get("traces_w"), rec.get("ref_traces_w")     # only with "save the averaged spectrum ..."
        return {"axis": self.grid_axis(rec),
                "rows": [self.grid_row(rec, i, on[i] if on else None, off[i] if off else None)
                         for i in range(len(rec["signal_frequency_hz"]))]}

    # ---- run ----
    def run(self, p: dict, job) -> None:
        plan = self.plan(p)
        if plan["errors"]:
            raise ValueError("; ".join(plan["errors"]))
        sig, _, _ = self.signal_points(p)
        biases, _, _ = self.biases(p)
        passes = 2 if self.with_ref(p) else 1
        mgr, owner = job.mgr, job.owner
        job.progress("reserving instruments", points_total=len(biases) * len(sig),
                     traces_total=len(biases) * len(sig) * passes * p["traces"])

        with mgr.claim(self.instruments, owner):
            snapshot = mgr.snapshot(owner=owner)
            for name in ("spa", PROBE, SYNTH) + ((NOISE,) if self.sweeps_noise(p) else ()) \
                    + ((ATT,) if p["att_mode"] == "set" else ()) + ((FLUX,) if p["flux_mode"] == "set" else ()):
                if "error" in snapshot.get(name, {}):
                    raise RuntimeError(f"cannot read {name}: {snapshot[name]['error']}")
            if self.sweeps_noise(p):
                err, _ = bias_setup(snapshot[NOISE], biases[0])
                if err:
                    raise RuntimeError(err)
            else:
                biases = [kept_bias(snapshot.get(NOISE))]
            rec = job.record
            rec.update(kind="experiment", experiment=self.id, params=self.recorded_params(p), note=p["note"],
                       sim=mgr.sim, fridge_temperature_k=p["fridge_temperature_k"],
                       pump_frequency_hz=p["pump_frequency_hz"], pump_power_dbm=p["pump_power_dbm"],
                       signal_frequency_hz=[], signal_power_dbm=[], noise_bias_v=[], traces=[], sa_center_hz=[],
                       point_start_time=[], point_end_time=[], **{k: [] for k in ON_KEYS}, snr_db=[],
                       instruments=snapshot, analysis_note=LOG_BIAS_NOTE)
            if self.with_ref(p):
                rec.update({f"ref_{k}": [] for k in ON_KEYS + ("snr_db", "start_time", "end_time")},
                           gain_db=[], noise_rise_db=[], snri_db=[])
            if p["save_traces"]:
                rec["traces_w"] = []
                if self.with_ref(p):
                    rec["ref_traces_w"] = []
            job.axis = self.grid_axis(rec)
            try:
                self._measure(p, job, sig, biases, snapshot)
            finally:
                self._finish(p, job, snapshot)

    def _pump(self, p, job, on: bool):
        """Pump output on or off, then wait. The first time it goes on, its state is saved."""
        mgr, owner, rec = job.mgr, job.owner, job.record
        job.progress(f"turning the TWPA pump {'on' if on else 'off'}")
        mgr.apply(SYNTH, owner, **{PUMP_ON: on})
        job.sleep(p["pump_settle_s"])
        if on and "synth_state" not in rec:
            st = mgr.get_state(SYNTH, owner)
            rec["synth_state"] = st
            if st.get("pump_locked") is False:
                job.notes.append("the SynthHD pump channel is not locked")

    def _point(self, p, job, f, pw, cur: dict, offsets, regions_args) -> tuple[dict, np.ndarray, float, float]:
        """One averaged spectrum at signal (f, pw): only what changed is sent (the SA center
        follows the frequency). Returns (analysis, spectrum in W, start, end)."""
        mgr, owner, off = job.mgr, job.owner, p["sa_offset_hz"]
        change = {k: v for k, v, old in (("probe_frequency_hz", f, cur["f"]), ("probe_power_dbm", pw, cur["pw"]))
                  if v != old}
        if change:
            mgr.apply(PROBE, owner, **change)
            if "probe_frequency_hz" in change:
                mgr.apply("spa", owner, center_hz=f + off)
            cur.update(f=f, pw=pw)
            job.sleep(p["settle_s"])
        bins = f + off + offsets
        t0 = time.time()
        p_w = self._spectrum(p, job, bins, p["traces"])
        t1 = time.time()
        a = analyze(bins, p_w, f + off, f + off, *regions_args)
        a["snr_db"] = ratio_db(a["signal_w"], a["noise_w"])
        if p_w.max() > dbm_to_w(p["ref_level_dbm"]):
            cur["clipped"].append(f"{f / 1e9:g} GHz / {pw:g} dBm")
        return a, p_w, t0, t1

    def _measure(self, p, job, sig, biases, snapshot):
        mgr, owner, rec = job.mgr, job.owner, job.record
        ref_on, noise = self.with_ref(p), self.sweeps_noise(p)
        if p["set_switches"]:
            job.progress("setting the warm switches")
            mgr.apply("warm_switches", owner, **SWITCH_PATH)
        if p["att_mode"] == "set":
            job.progress(f"setting the attenuator to {p['attenuation_db']:g} dB")
            mgr.apply(ATT, owner, attenuation_db=p["attenuation_db"])
        if p["flux_mode"] == "set":
            flux_start(job, snapshot[FLUX], [p["flux_bias_v"]])
        if noise:
            noise_start(job, snapshot[NOISE], biases[0])
        for name, key in ((ATT, "attenuator_state"), (FLUX, "flux_bias_state"), (NOISE, "noise_source_state")):
            try:
                rec[key] = mgr.get_state(name, owner)       # during the sweep (the noise source: at the first bias)
            except Exception as e:
                job.notes.append(f"could not read the {name}: {e}")

        f0, pw0 = sig[0]
        off = p["sa_offset_hz"]
        sa = self._setup_sa(p, job, f0 + off)
        offsets = sa_freqs(0.0, sa["span_hz"])
        inner, outer, window = p["noise_inner_hz"], self.noise_outer(p), self.window(p, sa["rbw_hz"])
        rec.update(sa_bin_offset_hz=offsets, sa_center_offset_hz=off, noise_inner_hz=inner, noise_outer_hz=outer,
                   signal_window_hz=window)
        job.axis = self.grid_axis(rec)
        regions_args = (inner, outer, window)

        # the pump: off for a pump-off pass, its frequency and power set before it goes on
        if ref_on:
            mgr.apply(SYNTH, owner, **{PUMP_ON: False})
        job.progress(f"setting the TWPA pump to {p['pump_frequency_hz'] / 1e9:g} GHz, {p['pump_power_dbm']:g} dBm")
        mgr.apply(SYNTH, owner, pump_frequency_hz=p["pump_frequency_hz"], pump_power_dbm=p["pump_power_dbm"])
        job.progress(f"turning the signal on at {f0 / 1e9:g} GHz, {pw0:g} dBm")
        mgr.apply(PROBE, owner, probe_frequency_hz=f0, probe_power_dbm=pw0)
        mgr.apply(PROBE, owner, **{PROBE_ON: True})
        rec["probe_state"] = mgr.get_state(PROBE, owner)
        job.sleep(p["settle_s"])
        pump_on = False
        if ref_on:
            job.sleep(p["pump_settle_s"])           # the pump may have been on before
        else:
            self._pump(p, job, True)
            pump_on = True

        cur = {"f": f0, "pw": pw0, "clipped": []}
        n = len(sig)
        for b, bias in enumerate(biases):
            job.check()
            at = f"noise {bias:g} V ({b + 1}/{len(biases)}), " if noise else ""
            if noise and b:
                job.progress(f"setting the noise source bias to {bias:g} V")
                mgr.apply(NOISE, owner, offset_v=bias)
            if noise:
                job.sleep(p["noise_settle_s"])
            ref = []
            if ref_on:
                if pump_on:
                    self._pump(p, job, False)
                    pump_on = False
                for j, (f, pw) in enumerate(sig):
                    job.check()
                    job.progress(f"{at}pump off, {self.point_text(f, pw)} ({j + 1}/{n})", points_done=b * n)
                    ref.append(self._point(p, job, f, pw, cur, offsets, regions_args))
            if not pump_on:
                self._pump(p, job, True)
                pump_on = True
            for j, (f, pw) in enumerate(sig):
                job.check()
                job.progress(f"{at}pump on, {self.point_text(f, pw)} ({j + 1}/{n})", points_done=b * n + j)
                a, p_w, t0, t1 = self._point(p, job, f, pw, cur, offsets, regions_args)
                for key, value in (("signal_frequency_hz", f), ("signal_power_dbm", pw), ("noise_bias_v", bias),
                                   ("traces", p["traces"]), ("sa_center_hz", f + off),
                                   ("point_start_time", round(t0, 3)), ("point_end_time", round(t1, 3)),
                                   *((k, a[k]) for k in ON_KEYS), ("snr_db", a["snr_db"])):
                    rec[key].append(value)
                ref_w = None
                if ref:
                    r, ref_w, r0, r1 = ref[j]
                    for k in ON_KEYS + ("snr_db",):
                        rec[f"ref_{k}"].append(r[k])
                    rec["ref_start_time"].append(round(r0, 3))
                    rec["ref_end_time"].append(round(r1, 3))
                    snri = (round(a["snr_db"] - r["snr_db"], 4)
                            if a["snr_db"] is not None and r["snr_db"] is not None else None)
                    rec["gain_db"].append(ratio_db(a["signal_w"], r["signal_w"]))
                    rec["noise_rise_db"].append(ratio_db(a["noise_w"], r["noise_w"]))
                    rec["snri_db"].append(snri)
                if p["save_traces"]:
                    rec["traces_w"].append(p_w)
                    if ref:
                        rec["ref_traces_w"].append(ref_w)
                i = len(rec["signal_frequency_hz"]) - 1
                saved = p["save_traces"]        # the live rows as the export rebuilds them from the record
                job.add_row(self.grid_row(rec, i, p_w if saved else None, ref_w if saved else None))
        weak = sum(s is None for s in rec["signal_w"])
        if weak:
            job.notes.append(f"the tone is not above the noise at {weak} of {len(rec['signal_w'])} points with the "
                             f"pump on (SNR null there)")
        if cur["clipped"]:
            job.notes.append(f"bins above the reference level ({p['ref_level_dbm']:g} dBm) in "
                             f"{len(cur['clipped'])} spectra, e.g. {cur['clipped'][0]}: the SA may have clipped; "
                             f"raise the reference level")
        job.progress(f"{len(biases) * n} points measured", points_done=len(biases) * n)

    @staticmethod
    def point_text(f: float, pw: float) -> str:
        return f"{f / 1e9:g} GHz, {pw:g} dBm"

    def _finish(self, p, job, snapshot):
        """Leave the instruments as asked. After "outputs off" nothing is sent: the
        outputs are already off and every command would be refused. Restoring the SA
        sets its old values; RBW/VBW auto coupling is not restored."""
        mgr, owner = job.mgr, job.owner
        if mgr.halt.is_set():
            return
        sa0, sw0, synth0 = snapshot.get("spa", {}), snapshot.get("warm_switches", {}), snapshot.get(SYNTH, {})
        att0, ns0, probe0 = snapshot.get(ATT, {}), snapshot.get(NOISE, {}), snapshot.get(PROBE, {})
        done = []
        try:
            if p["finish"] == "restore":
                if self.sweeps_noise(p) and "error" not in ns0:
                    noise_restore(self, job, ns0, done)
                if "error" not in synth0:
                    synth_restore(self, job, synth0, done, ch="pump")
                if "error" not in probe0:
                    synth_restore(self, job, probe0, done, ch="probe")
                if p["att_mode"] == "set" and "error" not in att0:
                    if within(self.limits(ATT).get("attenuation_db"), att0["attenuation_db"]):
                        mgr.apply(ATT, owner, attenuation_db=att0["attenuation_db"])
                        done.append("attenuator restored")
                    else:
                        job.notes.append(f"attenuator not restored: the old value {att0['attenuation_db']:g} dB "
                                         f"is outside the limits")
                if p["flux_mode"] == "set":
                    flux_finish(job, snapshot.get(FLUX, {}), "restore", self.limits(FLUX), done)
                self._restore_sa(job, sa0, done)
                if p["set_switches"] and "error" not in sw0:
                    mgr.apply("warm_switches", owner, **{k: sw0[k] for k in SWITCH_PATH if k in sw0})
                    done.append("switches restored")
            elif p["finish"] == "off":
                mgr.apply(SYNTH, owner, **{PUMP_ON: False})
                mgr.apply(PROBE, owner, **{PROBE_ON: False})
                done.append("TWPA pump and signal off")
                if self.sweeps_noise(p):
                    mgr.apply(NOISE, owner, offset_v=0.0)
                    mgr.apply(NOISE, owner, output=False)
                    done.append("noise source at 0 V, off")
                if p["flux_mode"] == "set":
                    flux_finish(job, snapshot.get(FLUX, {}), "off", self.limits(FLUX), done)
            with mgr.use("spa", owner) as spa:
                spa.idle()          # continuous sweep, so the front panel works again
        except Exception as e:
            job.notes.append(f"finish step failed: {e}")
            log.error("%s: finish step failed: %s", self.id, e)
        finally:
            if done:
                job.record["finish"] = done
