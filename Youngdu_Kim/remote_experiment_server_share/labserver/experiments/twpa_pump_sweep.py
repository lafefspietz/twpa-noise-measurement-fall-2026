"""TWPA pump sweep: TWPA gain and SNR improvement vs pump frequency and power, on the SA.

The SMW200A probe sends one tone through the fridge, and the SA takes a
spectrum (not zero span) around the SA frequency. The pump (channel B) is turned off
first for a reference spectrum, then stepped over a pump frequency x pump power grid
(frequency outer, power inner) with one averaged spectrum at each point. Either axis
may be a single value.

From each spectrum (traces averaged in W, sample detector, LOG scale):
  * noise  = mean of the bins with inner <= |f - f_SA| <= outer (both sides; outer
             empty = the edge of the span)
  * signal = max of the bins within +-window of the probe frequency (or of the SA
             frequency, by choice), minus the noise (the raw max is saved as peak_w)
and against the pump-off reference:
  * gain       = signal / signal_ref
  * noise rise = noise / noise_ref
  * SNRI       = (signal / noise) / (signal_ref / noise_ref) = gain / noise rise

With VBW << RBW the SA's video filter averages the log of the noise, so the noise reads
about 2.5 dB low while the tone does not. That cancels in the noise rise and the SNRI
(same setup pump on and off); the absolute SNR and the peak - noise subtraction carry it.

Signal path: fridge_in -> rf_probe, rx_source -> fridge, receiver -> spa (the same as
the Y factor measurement). Result: per point gain_db, snri_db, noise_rise_db, a 2D map
in the UI, with the newest spectrum below it.
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
from .sa_spectrum import (LOG_BIAS_NOTE, MIN_NOISE_BINS, SaSpectrumMixin, analyze,  # noqa: F401 (re-exported)
                          estimate_sweep_s, ratio_db, regions, sa_freqs, trace_dbm, w_to_dbm)
from .vna_sweep import sweep_values
from .y_factor import PROBE, PROBE_ON, PUMP_ON, SWITCH_PATH, SYNTH, TRACE_OVERHEAD_S, pump_text, synth_restore

log = logging.getLogger("labserver.experiments")

ATT = "attenuator"
PROBE_KEYS = ("probe_frequency_hz", "probe_power_dbm")
SWEEP = "Pump sweep"
TONE = "Probe tone (SMW200A)"
SA = "Spectrum analyzer (sample detector, LOG)"
ANALYSIS = "Signal and noise"
QUBIT = "Qubit flux bias"
END = "Before and after"
SAVE = "Save"


class TwpaPumpSweep(SaSpectrumMixin, Experiment):
    id = "twpa_pump_sweep"
    title = "TWPA pump sweep"
    summary = ("Step the TWPA pump over a frequency x power grid with a probe tone through the fridge, and "
               "take an SA spectrum at each point: gain, noise rise and SNR improvement against a pump-off "
               "reference. Result: 2D map, pump frequency x pump power.")
    instruments = ["spa", PROBE, SYNTH, "warm_switches", ATT, FLUX]
    turns_on = "the SMW200A probe and the TWPA pump (and the flux bias, if set for the sweep)"
    params = [
        Param("pump_freq_start_hz", "pump frequency from", unit="GHz", scale=1e9, default=8.0e9, group=SWEEP),
        Param("pump_freq_stop_hz", "to", unit="GHz", scale=1e9, default=9.0e9, group=SWEEP),
        Param("pump_freq_step_hz", "step", unit="GHz", scale=1e9, default=0.1e9, group=SWEEP,
              help="Outer loop. From = to gives one pump frequency (a power sweep). "
                   "Limit: rf_synth pump frequency in the settings tab."),
        Param("pump_power_start_dbm", "pump power from", unit="dBm", default=-6.0, group=SWEEP),
        Param("pump_power_stop_dbm", "to", unit="dBm", default=6.0, group=SWEEP),
        Param("pump_power_step_db", "step", unit="dB", default=1.0, group=SWEEP,
              help="Inner loop, from -> to at each pump frequency. Limit: rf_synth pump power in the settings tab."),
        Param("traces", "traces per point", kind="int", default=1, group=SWEEP,
              help="SA spectra averaged (in W) at each point, and for the reference."),
        Param("settle_s", "wait after each pump step", unit="s", default=0.1, group=SWEEP),
        Param("pump_settle_s", "wait after the pump goes off / on", unit="s", default=1.0, group=SWEEP,
              help="Before the pump-off reference, and again after the pump is first turned on."),
        # probe
        Param("probe_frequency_hz", "probe frequency", unit="GHz", scale=1e9, default=6.0e9, group=TONE,
              from_state=[PROBE, "probe_frequency_hz"],
              help="Must be inside the SA span and outside the noise region."),
        Param("probe_power_dbm", "probe power", unit="dBm", default=-20.0, group=TONE,
              from_state=[PROBE, "probe_power_dbm"],
              help="At the SMW200A output. Limit: rf_probe probe power in the settings tab."),
        Param("att_mode", "warm step attenuator", kind="select", default="keep", group=TONE,
              options=[["keep", "leave it as it is"], ["set", "set it for the sweep"]],
              help="RCDAT between fridge_in and the fridge, in the probe path."),
        Param("attenuation_db", "attenuation", unit="dB", default=25.0, group=TONE,
              show_if={"att_mode": ["set"]}, from_state=[ATT, "attenuation_db"],
              help="Multiples of 0.25 dB. Limit: attenuator attenuation in the settings tab."),
        # SA
        Param("center_hz", "SA frequency (center)", unit="GHz", scale=1e9, default=6.0e9, group=SA,
              from_state=["spa", "center_hz"],
              help="The noise region is set around this frequency."),
        Param("span_hz", "span", unit="MHz", scale=1e6, default=5e6, group=SA, from_state=["spa", "span_hz"],
              help="401 bins across the span."),
        Param("rbw_hz", "RBW", unit="kHz", scale=1e3, default=30e3, group=SA, from_state=["spa", "rbw_hz"],
              help="The ESA takes 1-3-10 steps (RBW and VBW): the run reads back the values it took and saves them."),
        Param("vbw_hz", "VBW", unit="kHz", scale=1e3, default=1e3, group=SA, from_state=["spa", "vbw_hz"]),
        Param("sweep_time_s", "sweep time", unit="s", default=None, group=SA,
              help="Empty = the SA's auto sweep time (the plan estimate is rough; the run saves the real one)."),
        Param("ref_level_dbm", "reference level", unit="dBm", default=-40.0, group=SA,
              help="Keep the tone below it at the highest gain, or the SA clips."),
        Param("save_traces", "save the averaged spectrum of every point", kind="bool", default=True, group=SA),
        # analysis
        Param("noise_inner_hz", "noise from", unit="MHz", scale=1e6, default=1e6, group=ANALYSIS,
              help="Noise = mean (in W) of the bins at least this far from the SA frequency, on both sides."),
        Param("noise_outer_hz", "noise to", unit="MHz", scale=1e6, default=None, group=ANALYSIS,
              help="... and at most this far. Empty = to the edge of the span."),
        Param("signal_center", "signal window around", kind="select", default="probe", group=ANALYSIS,
              options=[["probe", "the probe frequency"], ["sa", "the SA frequency"]],
              help="Without a shared 10 MHz reference the SMW200A and the SA disagree a little, so the tone "
                   "lands a little off the probe frequency as the SA sees it. Centre the window where the "
                   "tone actually shows up, and make it wide enough for the offset."),
        Param("signal_window_hz", "signal window (+-)", unit="kHz", scale=1e3, default=None, group=ANALYSIS,
              help="Signal = max of the bins within +- this of the frequency above, minus the noise. "
                   "Empty = the RBW the SA takes."),
        # qubit
        Param("flux_mode", "qubit flux bias", kind="select", default="keep", group=QUBIT,
              options=[["keep", "leave it as it is"], ["set", "set it for the sweep"]],
              help="Keep the qubit away from the probe frequency. Either way it is reserved during the "
                   "sweep and its state is saved."),
        Param("flux_bias_v", "flux bias", unit="V", default=0.0, group=QUBIT, show_if={"flux_mode": ["set"]},
              from_state=[FLUX, "level"],
              help="Limit: qubit_flux_bias level (VOLT mode) in the settings tab."),
        # before / after
        Param("set_switches", "set the warm switches to SMW200A probe -> fridge -> SA first", kind="bool",
              default=True, group=END,
              help="fridge_in -> rf_probe, rx_source -> fridge, receiver -> spa."),
        Param("finish", "when done", kind="select", default="restore", group=END,
              options=[["restore", "restore the SA, switches, attenuator, probe, pump and flux bias as they were"],
                       ["last", "leave everything at the last point"],
                       ["off", "turn the probe and the pump off (and the flux bias, ramped to 0 V, if the "
                               "sweep set it); the SA, switches and attenuator stay"]]),
        # save
        Param("fridge_temperature_k", "fridge temperature (for the record)", unit="K", default=None, group=SAVE),
        Param("label", "label", kind="text", default="twpa-pump-sweep", group=SAVE),
        Param("note", "note", kind="text", default="", group=SAVE),
    ]

    # ---- plan ----
    def points(self, p: dict) -> tuple[list[tuple[float, float]], list[str], list[str]]:
        """[(pump frequency, pump power)] in sweep order, plus (errors, warnings)."""
        errors, warnings = [], []
        axes = []
        for what, start, stop, step, unit, scale in (
                ("pump frequency", p["pump_freq_start_hz"], p["pump_freq_stop_hz"], p["pump_freq_step_hz"], "GHz", 1e9),
                ("pump power", p["pump_power_start_dbm"], p["pump_power_stop_dbm"], p["pump_power_step_db"], "dBm", 1)):
            if start == stop:
                axes.append([start])
            elif step <= 0:
                errors.append(f"{what} step must be > 0")
            elif abs(stop - start) / step + 1 > self.cap("max_points"):
                errors.append(f"more than {self.cap('max_points')} {what} points; use a larger step")
            else:
                values, warn = sweep_values(start / scale, stop / scale, step / scale, unit)
                axes.append([round(v * scale, 3) for v in values])
                if warn:
                    warnings.append(f"{what}: {warn}")
        if p["traces"] < 1:
            errors.append("traces per point must be >= 1")
        if errors:
            return [], errors, warnings
        points = [(f, pw) for f in axes[0] for pw in axes[1]]
        if len(points) > self.cap("max_total_points"):
            errors.append(f"more than {self.cap('max_total_points')} points in all; use larger steps")
        return points, errors, warnings

    @staticmethod
    def noise_outer(p: dict) -> float:
        return p["noise_outer_hz"] if p["noise_outer_hz"] is not None else p["span_hz"] / 2

    @staticmethod
    def window(p: dict, rbw_hz: float | None = None) -> float:
        return p["signal_window_hz"] if p["signal_window_hz"] is not None else (rbw_hz or p["rbw_hz"])

    @staticmethod
    def signal_center_hz(p: dict, f_sa: float | None = None) -> float:
        """Center of the signal window: the probe frequency, or the SA frequency (f_sa: as
        the SA took it)."""
        return p["probe_frequency_hz"] if p["signal_center"] == "probe" else (f_sa or p["center_hz"])

    def check_regions(self, p: dict, errors: list[str], warnings: list[str], rbw_hz: float | None = None):
        """The noise region and the signal window on the SA's bins."""
        if p["span_hz"] <= 0:
            errors.append("span must be > 0 (this takes a spectrum, not zero span)")
            return
        inner, outer, window = p["noise_inner_hz"], self.noise_outer(p), self.window(p, rbw_hz)
        f_sa, f_probe, half = p["center_hz"], p["probe_frequency_hz"], p["span_hz"] / 2
        if inner < 0 or outer <= inner:
            errors.append("noise region: 'noise from' must be >= 0 and below 'noise to'")
            return
        if outer > half * (1 + 1e-9):
            errors.append(f"noise region: 'noise to' {outer / 1e6:g} MHz is beyond the edge of the span "
                          f"({half / 1e6:g} MHz from the center)")
            return
        if window <= 0:
            errors.append("signal window must be > 0")
            return
        freqs = sa_freqs(f_sa, p["span_hz"])
        f_sig = self.signal_center_hz(p)
        noise, signal = regions(freqs, f_sa, f_sig, inner, outer, window)
        per_side = min(int((noise & (freqs < f_sa)).sum()), int((noise & (freqs > f_sa)).sum()))
        if per_side < MIN_NOISE_BINS:
            errors.append(f"noise region has {per_side} bins on one side (at least {MIN_NOISE_BINS}); "
                          f"widen it or the span")
        if abs(f_probe - f_sa) > half:
            errors.append(f"probe frequency {f_probe / 1e9:g} GHz is outside the SA span")
        elif (noise & signal).any():
            errors.append("the signal window falls in the noise region; move the probe or the noise region")
        elif (noise & (np.abs(freqs - f_probe) <= window)).any():
            errors.append("the probe tone (+- signal window) falls in the noise region and would raise the noise; "
                          "move the probe or the noise region")
        elif noise.any() and np.abs(freqs[noise] - f_sig).min() < 3 * (rbw_hz or p["rbw_hz"]):
            warnings.append("the noise region starts within 3 RBW of the signal window center: the tone's skirt "
                            "may raise the noise")
        if p["signal_center"] == "sa" and abs(f_probe - f_sa) > window:
            warnings.append(f"the probe is {abs(f_probe - f_sa) / 1e3:g} kHz from the SA frequency, outside the "
                            f"signal window (+-{window / 1e3:g} kHz) around it")
        bin_hz = p["span_hz"] / (N_POINTS - 1)
        if window < bin_hz / 2:
            warnings.append(f"signal window +-{window / 1e3:g} kHz is narrower than the bin spacing "
                            f"({bin_hz / 1e3:g} kHz): the nearest bin is used")
        if bin_hz > (rbw_hz or p["rbw_hz"]):
            warnings.append(f"bin spacing {bin_hz / 1e3:g} kHz is wider than the RBW: the bins may miss the "
                            f"top of the tone; use a narrower span or a wider RBW")

    def plan(self, p: dict, mgr=None) -> dict:
        points, errors, warnings = self.points(p)
        notes = []
        self.check_limit(errors, SYNTH, "pump_frequency_hz", sorted({f for f, _ in points}), "pump frequency", "Hz")
        self.check_limit(errors, SYNTH, "pump_power_dbm", sorted({pw for _, pw in points}), "pump power", "dBm")
        self.check_limit(errors, PROBE, "probe_frequency_hz", p["probe_frequency_hz"], "probe frequency", "Hz")
        self.check_limit(errors, PROBE, "probe_power_dbm", p["probe_power_dbm"], "probe power", "dBm")
        if points and not all(10e6 <= f <= 15e9 for f, _ in points):
            errors.append("pump frequency must be within 0.01..15 GHz (SynthHD)")
        if points and not all(-80 <= pw <= 20 for _, pw in points):
            errors.append("pump power must be within -80..20 dBm (SynthHD)")
        if not PROBE_FREQ_HZ[0] <= p["probe_frequency_hz"] <= PROBE_FREQ_HZ[1]:
            errors.append("probe frequency must be within 0.0001..44 GHz (SMW200A)")
        if not PROBE_POWER_DBM[0] <= p["probe_power_dbm"] <= PROBE_POWER_DBM[1]:
            errors.append("probe power must be within -145..30 dBm (SMW200A)")
        if p["att_mode"] == "set":
            self.check_limit(errors, ATT, "attenuation_db", p["attenuation_db"], "attenuation", "dB")
            if abs(p["attenuation_db"] / ATT_STEP_DB - round(p["attenuation_db"] / ATT_STEP_DB)) > 1e-9:
                errors.append(f"attenuation not a multiple of {ATT_STEP_DB:g} dB")
        if p["flux_mode"] == "set":
            self.check_limit(errors, FLUX, "level", p["flux_bias_v"], "flux bias", "V")
        if p["rbw_hz"] <= 0 or p["vbw_hz"] <= 0:
            errors.append("RBW and VBW must be > 0")
        elif p["center_hz"] <= 0:
            errors.append("SA frequency must be > 0")
        else:
            self.check_regions(p, errors, warnings)
        if p["sweep_time_s"] is not None and p["sweep_time_s"] <= 0:
            errors.append("sweep time must be > 0; leave it empty for the SA's auto sweep time")
        if p["settle_s"] < 0 or p["pump_settle_s"] < 0:
            errors.append("waits must be >= 0")
        for key, value, what, unit in (("center_hz", p["center_hz"], "SA frequency", "Hz"),
                                       ("span_hz", p["span_hz"], "SA span", "Hz"),
                                       ("rbw_hz", p["rbw_hz"], "RBW", "Hz"), ("vbw_hz", p["vbw_hz"], "VBW", "Hz"),
                                       ("sweep_time_s", p["sweep_time_s"], "sweep time", "s"),
                                       ("ref_level_dbm", p["ref_level_dbm"], "SA reference level", "dBm")):
            if value is not None:
                self.check_limit(errors, "spa", key, value, what, unit)

        sa_avg = 1
        if mgr is not None:
            sa_avg = self.check_instruments(mgr, p, points, errors, warnings, notes)
        sweep_s = p["sweep_time_s"]
        if sweep_s is None and p["rbw_hz"] > 0 and p["vbw_hz"] > 0 and p["span_hz"] > 0:
            sweep_s = estimate_sweep_s(p["span_hz"], p["rbw_hz"], p["vbw_hz"])
        traces = (len(points) + 1) * p["traces"]
        estimate_s = (traces * ((sweep_s or 0) * sa_avg + TRACE_OVERHEAD_S) + len(points) * max(p["settle_s"], 0)
                      + 2 * max(p["pump_settle_s"], 0))
        size_mb = ((len(points) + 1) * (14 + (N_POINTS if p["save_traces"] else 0)) + 2 * N_POINTS) * 26 / 1e6
        if size_mb > self.cap("big_file_mb"):
            warnings.append(f"the data file would be about {size_mb:.0f} MB; turn off saving every spectrum")
        if p["sweep_time_s"] is None and sweep_s:
            notes.append(f"sweep time about {sweep_s:.2g} s per spectrum (rough; SA auto)")
        if points:
            nf, npw = len({f for f, _ in points}), len({pw for _, pw in points})
            notes.append(f"pump-off reference, then {nf} pump frequencies x {npw} pump powers = {len(points)} "
                         f"points, {traces} spectra in all")
        return {"points": [{"i": i, "pump_frequency_ghz": round(f / 1e9, 9), "pump_power_dbm": pw,
                            "traces": p["traces"]} for i, (f, pw) in enumerate(points)],
                "estimate_s": round(estimate_s, 1), "size_mb": round(size_mb, 2),
                "columns": [["i", "#", ""], ["pump_frequency_ghz", "pump frequency", "GHz"],
                            ["pump_power_dbm", "pump power", "dBm"], ["traces", "traces", ""]],
                "errors": errors, "warnings": warnings, "notes": notes}

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
                warnings.append(f"the SA's own averaging is on: each spectrum is {sa_avg} sweeps")
            if sa.get("attenuation_db"):
                notes.append(f"SA input attenuation is {sa['attenuation_db']:g} dB (not changed)")
        except Exception as e:
            warnings.append(f"could not read the SA: {e}")

        try:
            probe = self.read_state(mgr, PROBE)
            now = (f"on at {probe['probe_frequency_hz'] / 1e9:g} GHz, {probe['probe_power_dbm']:g} dBm"
                   if probe.get(PROBE_ON) else "off")
            notes.append(f"SMW200A probe is {now}: set to {p['probe_frequency_hz'] / 1e9:g} GHz, "
                         f"{p['probe_power_dbm']:g} dBm and on")
        except Exception as e:
            errors.append(f"could not read the probe (SMW200A): {e}")
        try:
            synth = self.read_state(mgr, SYNTH)
            notes.append(f"TWPA pump is {pump_text(synth)}: off for the reference, then swept"
                         + (", back as it was after" if p["finish"] == "restore" else ""))
        except Exception as e:
            errors.append(f"could not read the SynthHD: {e}")

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
    @staticmethod
    def grid_axis(rec: dict) -> dict:
        """Plot axis for the record so far: the grid, then the SA bins and regions once the
        SA is set up, then the pump-off reference."""
        ax = {"plot": "grid", "x_key": "pump_frequency_hz", "x_label": "pump frequency", "x_unit": "GHz",
              "x_scale": 1e9, "y_key": "pump_power_dbm", "y_label": "pump power", "y_unit": "dBm"}
        sa = rec.get("spa_setup")
        if sa and "frequency_hz" in rec:
            f_sa, f_probe = sa["center_hz"], rec["probe_frequency_hz"]
            f_sig = rec.get("signal_center_hz", f_probe)       # records before the choice: the probe
            ax.update(f=np.asarray(rec["frequency_hz"], dtype=float).tolist(), f_sa=f_sa, f_probe=f_probe,
                      f_signal=f_sig, noise_inner_hz=rec["noise_inner_hz"], noise_outer_hz=rec["noise_outer_hz"],
                      window_hz=rec["signal_window_hz"],
                      title=(f"probe {f_probe / 1e9:g} GHz, SA {f_sa / 1e9:g} GHz, "
                             f"signal window around the {'SA' if rec['params'].get('signal_center') == 'sa' else 'probe'}, "
                             f"span {sa['span_hz'] / 1e6:g} MHz, "
                             f"RBW {sa['rbw_hz'] / 1e3:g} kHz, VBW {sa['vbw_hz'] / 1e3:g} kHz, "
                             f"sweep {sa['sweep_time_s']:g} s"))
        ref = rec.get("reference")
        if ref is not None:
            ax["ref"] = {"trace_dbm": trace_dbm(ref["trace_w"]), "signal_dbm": w_to_dbm(ref["signal_w"]),
                         "noise_dbm": w_to_dbm(ref["noise_w"]), "snr_db": ref["snr_db"]}
        return ax

    @staticmethod
    def grid_row(rec: dict, i: int, p_w=None) -> dict:
        """Result row of point i; p_w (its spectrum in W) adds trace_dbm."""
        row = {"i": i, **{k: rec[k][i] for k in ("pump_frequency_hz", "pump_power_dbm", "traces", "gain_db",
                                                 "snri_db", "noise_rise_db", "snr_db")},
               "signal_dbm": w_to_dbm(rec["signal_w"][i]), "noise_dbm": w_to_dbm(rec["noise_w"][i]),
               "peak_hz": rec["peak_hz"][i]}
        if p_w is not None:
            row["trace_dbm"] = trace_dbm(p_w)
        return row

    def result_view(self, rec: dict) -> dict:
        traces = rec.get("traces_w")        # only with "save the averaged spectrum of every point"
        return {"axis": self.grid_axis(rec),
                "rows": [self.grid_row(rec, i, traces[i] if traces else None)
                         for i in range(len(rec["pump_frequency_hz"]))]}

    # ---- run ----
    def run(self, p: dict, job) -> None:
        plan = self.plan(p)
        if plan["errors"]:
            raise ValueError("; ".join(plan["errors"]))
        points, _, _ = self.points(p)
        mgr, owner = job.mgr, job.owner
        job.progress("reserving instruments", points_total=len(points), traces_total=(len(points) + 1) * p["traces"])

        with mgr.claim(self.instruments, owner):
            snapshot = mgr.snapshot(owner=owner)
            for name in ("spa", PROBE, SYNTH) + ((ATT,) if p["att_mode"] == "set" else ()) \
                    + ((FLUX,) if p["flux_mode"] == "set" else ()):
                if "error" in snapshot.get(name, {}):
                    raise RuntimeError(f"cannot read {name}: {snapshot[name]['error']}")
            rec = job.record
            rec.update(kind="experiment", experiment=self.id, params=self.recorded_params(p), note=p["note"],
                       sim=mgr.sim, fridge_temperature_k=p["fridge_temperature_k"],
                       probe_frequency_hz=p["probe_frequency_hz"], sa_frequency_hz=p["center_hz"],
                       pump_frequency_hz=[], pump_power_dbm=[], traces=[], point_start_time=[], point_end_time=[],
                       signal_w=[], peak_w=[], peak_hz=[], noise_w=[], noise_std_w=[],
                       gain_db=[], noise_rise_db=[], snri_db=[], snr_db=[], instruments=snapshot,
                       analysis_note=LOG_BIAS_NOTE)
            if p["save_traces"]:
                rec["traces_w"] = []
            job.axis = self.grid_axis(rec)
            try:
                self._measure(p, job, points, snapshot)
            finally:
                self._finish(p, job, snapshot)

    def _measure(self, p, job, points, snapshot):
        mgr, owner, rec = job.mgr, job.owner, job.record
        if p["set_switches"]:
            job.progress("setting the warm switches")
            mgr.apply("warm_switches", owner, **SWITCH_PATH)
        if p["att_mode"] == "set":
            job.progress(f"setting the attenuator to {p['attenuation_db']:g} dB")
            mgr.apply(ATT, owner, attenuation_db=p["attenuation_db"])
        if p["flux_mode"] == "set":
            flux_start(job, snapshot[FLUX], [p["flux_bias_v"]])
        for name, key in ((ATT, "attenuator_state"), (FLUX, "flux_bias_state")):
            try:
                rec[key] = mgr.get_state(name, owner)       # during the sweep
            except Exception as e:
                job.notes.append(f"could not read the {name}: {e}")

        sa = self._setup_sa(p, job, p["center_hz"])
        freqs = sa_freqs(sa["center_hz"], sa["span_hz"])
        f_sa, f_probe = sa["center_hz"], p["probe_frequency_hz"]
        f_sig = self.signal_center_hz(p, f_sa)
        inner, outer, window = p["noise_inner_hz"], self.noise_outer(p), self.window(p, sa["rbw_hz"])
        rec.update(frequency_hz=freqs, noise_inner_hz=inner, noise_outer_hz=outer, signal_window_hz=window,
                   signal_center_hz=f_sig)
        job.axis = self.grid_axis(rec)

        job.progress(f"turning the probe on at {f_probe / 1e9:g} GHz, {p['probe_power_dbm']:g} dBm")
        mgr.apply(SYNTH, owner, **{PUMP_ON: False})
        mgr.apply(PROBE, owner, probe_frequency_hz=f_probe, probe_power_dbm=p["probe_power_dbm"])
        mgr.apply(PROBE, owner, **{PROBE_ON: True})
        rec["probe_state"] = mgr.get_state(PROBE, owner)
        job.sleep(p["pump_settle_s"])

        # pump-off reference
        job.progress(f"pump-off reference, {p['traces']} traces")
        t0 = time.time()
        ref_w = self._spectrum(p, job, freqs, p["traces"])
        ref = analyze(freqs, ref_w, f_sa, f_sig, inner, outer, window)
        ref.update(snr_db=ratio_db(ref["signal_w"], ref["noise_w"]), start_time=round(t0, 3),
                   end_time=round(time.time(), 3), trace_w=ref_w)
        rec["reference"] = ref
        job.axis = self.grid_axis(rec)
        if ref["signal_w"] is None:
            job.notes.append("the probe tone is not above the noise in the pump-off reference: gain and SNRI "
                             "are undefined; check the probe power, the switches and the probe frequency")
        clipped = [] if ref_w.max() <= dbm_to_w(p["ref_level_dbm"]) else ["reference"]

        # pump on, then the grid
        f0, pw0 = points[0]
        job.progress(f"turning the TWPA pump on at {f0 / 1e9:g} GHz, {pw0:g} dBm")
        mgr.apply(SYNTH, owner, pump_frequency_hz=f0, pump_power_dbm=pw0)
        mgr.apply(SYNTH, owner, **{PUMP_ON: True})
        job.sleep(p["pump_settle_s"])
        st = mgr.get_state(SYNTH, owner)
        rec["synth_state"] = st
        if st.get("pump_locked") is False:
            job.notes.append("the SynthHD pump channel is not locked")
        last = (f0, pw0)
        for i, (f, pw) in enumerate(points):
            job.check()
            job.progress(f"pump {f / 1e9:g} GHz, {pw:g} dBm ({i + 1}/{len(points)})", points_done=i)
            change = {k: v for k, v, old in (("pump_frequency_hz", f, last[0]), ("pump_power_dbm", pw, last[1]))
                      if v != old}
            if change:
                mgr.apply(SYNTH, owner, **change)
                last = (f, pw)
                job.sleep(p["settle_s"])
            t0 = time.time()
            p_w = self._spectrum(p, job, freqs, p["traces"])
            t1 = time.time()
            a = analyze(freqs, p_w, f_sa, f_sig, inner, outer, window)
            gain = ratio_db(a["signal_w"], ref["signal_w"])
            rise = ratio_db(a["noise_w"], ref["noise_w"])
            snr = ratio_db(a["signal_w"], a["noise_w"])
            snri = round(snr - ref["snr_db"], 4) if snr is not None and ref["snr_db"] is not None else None
            if p_w.max() > dbm_to_w(p["ref_level_dbm"]):
                clipped.append(f"{f / 1e9:g} GHz / {pw:g} dBm")
            for key, value in (("pump_frequency_hz", f), ("pump_power_dbm", pw), ("traces", p["traces"]),
                               ("point_start_time", round(t0, 3)), ("point_end_time", round(t1, 3)),
                               ("signal_w", a["signal_w"]), ("peak_w", a["peak_w"]), ("peak_hz", a["peak_hz"]),
                               ("noise_w", a["noise_w"]), ("noise_std_w", a["noise_std_w"]), ("gain_db", gain),
                               ("noise_rise_db", rise), ("snri_db", snri), ("snr_db", snr)):
                rec[key].append(value)
            if p["save_traces"]:
                rec["traces_w"].append(p_w)
            job.add_row(self.grid_row(rec, i, p_w))
        if clipped:
            job.notes.append(f"bins above the reference level ({p['ref_level_dbm']:g} dBm) in {len(clipped)} "
                             f"spectra, e.g. {clipped[0]}: the SA may have clipped; raise the reference level")
        job.progress(f"{len(points)} points measured", points_done=len(points))

    def _finish(self, p, job, snapshot):
        """Leave the instruments as asked. After "outputs off" nothing is sent: the
        outputs are already off and every command would be refused. Restoring the SA
        sets its old values; RBW/VBW auto coupling is not restored."""
        mgr, owner = job.mgr, job.owner
        if mgr.halt.is_set():
            return
        sa0, sw0, synth0 = snapshot.get("spa", {}), snapshot.get("warm_switches", {}), snapshot.get(SYNTH, {})
        att0, probe0 = snapshot.get(ATT, {}), snapshot.get(PROBE, {})
        done = []
        try:
            if p["finish"] == "restore":
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
                done.append("TWPA pump and probe off")
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
