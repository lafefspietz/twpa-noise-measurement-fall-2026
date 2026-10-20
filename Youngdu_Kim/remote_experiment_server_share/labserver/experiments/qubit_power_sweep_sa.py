"""Qubit power sweep on the spectrum analyzer: the qubit power sweep with the SMW200A probe
as the source and the SA as the receiver, instead of the VNA.

Steps the drive power in one of two ways, like the VNA version:
  * "synth": steps the SMW200A probe power. The warm attenuator is left as it
    is or set once for the sweep.
  * "attenuator": keeps the probe power fixed and steps the warm programmable attenuator
    (RCDAT) instead. It is set to the first point before the probe is turned on, and on
    restore raised before it is lowered, so the drive never exceeds both the sweep's and
    the old one.

At each power the probe frequency is stepped across the qubit, and the SA, centred on the
probe frequency plus a fixed offset (sa_offset_hz, for SMW200A and SA clocks that are not
locked to one 10 MHz reference) with a narrow span, takes an averaged spectrum (traces
averaged in W, sample detector, LOG scale) at every frequency. From it
(sa_spectrum.analyze): the tone peak within +-window of the SA center, and the noise in
the bins at least noise_inner_hz from it. Power is outer (low drive first, segments with
more traces at low power), frequency inner.

Absolute power only: there is no reference measurement (qubit detuned or off resonance);
normalise in the analysis. The map shows peak - probe power in dB, which, like the VNA
S21, includes the warm attenuator; in attenuator mode the plot adds it back (tone - drive).

Signal path: fridge_in -> rf_probe, rx_source -> fridge, receiver -> spa (as in the TWPA
pump sweep). The TWPA pump is left as it is (its state is in the saved snapshot).

Result: per power drive_dbm (probe power - attenuation, at fridge_in) and optionally
qubit_power_dbm (drive - the line attenuation, if given); peak_w, peak_hz, signal_w,
noise_w, noise_std_w[power][frequency]; a 2D map in the UI, frequency x power.
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
from .qubit_power_sweep import segment_points
from .sa_spectrum import (LOG_BIAS_NOTE, SaSpectrumMixin, analyze, check_center_offset, check_centered_regions,
                          estimate_sweep_s, sa_freqs)
from .vna_sweep import sweep_values
from .y_factor import PROBE, PROBE_ON, SWITCH_PATH, SYNTH, TRACE_OVERHEAD_S, pump_text, synth_restore

log = logging.getLogger("labserver.experiments")

ATT = "attenuator"
STEP_OVERHEAD_S = 0.15      # probe frequency + SA center (each apply reads the state back), rough

SWEEP = "Power sweep"
FREQ = "Probe frequency (SMW200A)"
SA = "Spectrum analyzer (sample detector, LOG, centred on the probe)"
ANALYSIS = "Tone and noise"
QUBIT = "Qubit flux bias"
END = "Before and after"
SAVE = "Save"


def power_row(key: str, value: float, averages: int, peak_w, source_dbm: float) -> dict:
    """Result row of one power: the tone peak - probe power in dB at each frequency."""
    peak_dbm = 10 * np.log10(np.maximum(np.asarray(peak_w, dtype=float), 1e-30) / 1e-3)
    return {key: value, "averages": averages, "s_db": np.round(peak_dbm - source_dbm, 4).tolist()}


class QubitPowerSweepSA(SaSpectrumMixin, Experiment):
    id = "qubit_power_sweep_sa"
    title = "Qubit power sweep (SA)"
    summary = ("Step the drive power (SMW200A probe power, or the warm step attenuator at a fixed probe "
               "power) and at each power step the probe frequency across the qubit, reading the transmitted "
               "tone on the SA (narrow span centred on the probe). Absolute tone power, no reference. "
               "Result: 2D map, frequency x power (or attenuation).")
    instruments = ["spa", PROBE, SYNTH, "warm_switches", ATT, FLUX]     # SYNTH: the pump stays as it is
    turns_on = "the SMW200A probe (and the flux bias, if set for the sweep)"
    params = [
        Param("power_control", "step the power with", kind="select", default="synth", group=SWEEP,
              options=[["synth", "SMW200A probe power"],
                       [ATT, "warm step attenuator (probe power fixed)"]],
              help="Step attenuator: the probe power stays fixed and the warm RCDAT (0-63 dB in 0.25 dB "
                   "steps) is stepped instead."),
        Param("segments", "probe power segments", kind="table", group=SWEEP, show_if={"power_control": ["synth"]},
              default=[{"start_dbm": -60.0, "stop_dbm": -40.0, "step_db": 2.0, "averages": 4},
                       {"start_dbm": -40.0, "stop_dbm": -20.0, "step_db": 1.0, "averages": 2},
                       {"start_dbm": -20.0, "stop_dbm": 0.0, "step_db": 2.0, "averages": 1}],
              columns=[Param("start_dbm", "from", unit="dBm"), Param("stop_dbm", "to", unit="dBm"),
                       Param("step_db", "step", unit="dB"),
                       Param("averages", "traces per point", kind="int")],
              help="SMW200A probe power, swept top to bottom (low power first; it is noisier, so give it more "
                   "SA traces per point). Limit: rf_probe probe power in the settings tab."),
        Param("att_mode", "warm step attenuator", kind="select", default="keep", group=SWEEP,
              show_if={"power_control": ["synth"]},
              options=[["keep", "leave it as it is"], ["set", "set it for the sweep"]],
              help="RCDAT between fridge_in and the fridge, in the probe path. Either way it is reserved and "
                   "drive_dbm = probe power - its attenuation."),
        Param("attenuation_db", "attenuation", unit="dB", default=25.0, group=SWEEP,
              show_if={"power_control": ["synth"], "att_mode": ["set"]}, from_state=[ATT, "attenuation_db"],
              help="Multiples of 0.25 dB. Limit: attenuator attenuation in the settings tab."),
        Param("synth_power_dbm", "SMW200A probe power (fixed)", unit="dBm", default=-10.0, group=SWEEP,
              show_if={"power_control": [ATT]},
              help="Kept for the whole sweep. Drive after the attenuator = this - attenuation."),
        Param("att_segments", "attenuation segments", kind="table", group=SWEEP,
              show_if={"power_control": [ATT]},
              default=[{"start_db": 63.0, "stop_db": 45.0, "step_db": 2.0, "averages": 4},
                       {"start_db": 45.0, "stop_db": 25.0, "step_db": 1.0, "averages": 2},
                       {"start_db": 25.0, "stop_db": 15.0, "step_db": 2.0, "averages": 1}],
              columns=[Param("start_db", "from", unit="dB"), Param("stop_db", "to", unit="dB"),
                       Param("step_db", "step", unit="dB"),
                       Param("averages", "traces per point", kind="int")],
              help="Swept top to bottom: start at high attenuation (low power). Multiples of 0.25 dB. "
                   "Limit: attenuator attenuation in the settings tab."),
        Param("line_atten_db", "line attenuation fridge_in -> qubit (optional)", unit="dB", default=None,
              group=SWEEP,
              help="If given, the record also has qubit_power_dbm = drive_dbm - this, for each power. Not "
                   "counting the warm attenuator, and for the SMW200A path (the VNA fit gave 102.4 dB from "
                   "the VNA port with the attenuator at 25 dB). Empty = not computed."),
        # probe frequency
        Param("freq_start_hz", "probe frequency from", unit="GHz", scale=1e9, default=5.555e9, group=FREQ),
        Param("freq_stop_hz", "to", unit="GHz", scale=1e9, default=5.605e9, group=FREQ),
        Param("freq_step_hz", "step", unit="MHz", scale=1e6, default=1e6, group=FREQ,
              help="Inner loop, from -> to at each power. From = to gives one frequency (a power cut). "
                   "Limit: rf_probe probe frequency in the settings tab."),
        Param("settle_s", "wait after each frequency step", unit="s", default=0.05, group=FREQ,
              help="Also after each power step and after the probe is turned on."),
        # SA
        Param("sa_offset_hz", "SA center - probe frequency", unit="kHz", scale=1e3, default=0.0, group=SA,
              help="The SA center at every point is the probe frequency plus this. Without a shared 10 MHz "
                   "reference the tone shows up a little off the probe frequency on the SA: set this to where "
                   "it shows up (e.g. read it off the live SA view with the probe on), and the tone window and "
                   "the noise region follow. The clock error is in ppm, so it changes a little across the "
                   "sweep (2 ppm over 50 MHz: 100 Hz); keep the tone window wide enough for that."),
        Param("span_hz", "span", unit="kHz", scale=1e3, default=100e3, group=SA,
              help="Centred on the SA center (probe frequency + offset) at every point; 401 bins across it."),
        Param("rbw_hz", "RBW", unit="kHz", scale=1e3, default=1e3, group=SA,
              help="The ESA takes 1-3-10 steps (RBW and VBW): the run reads back the values it took and saves them."),
        Param("vbw_hz", "VBW", unit="kHz", scale=1e3, default=1e3, group=SA),
        Param("sweep_time_s", "sweep time", unit="s", default=None, group=SA,
              help="Empty = the SA's auto sweep time (the plan estimate is rough; the run saves the real one)."),
        Param("ref_level_dbm", "reference level", unit="dBm", default=-40.0, group=SA,
              help="Keep the tone below it at the highest drive, or the SA clips."),
        Param("save_traces", "save the averaged spectrum of every point", kind="bool", default=False, group=SA,
              help="401 numbers per point: the file gets big quickly."),
        # analysis
        Param("signal_window_hz", "tone window (+-)", unit="kHz", scale=1e3, default=15e3, group=ANALYSIS,
              help="Tone = max of the bins within +- this of the SA center (probe frequency + offset). "
                   "Without a shared 10 MHz reference the SMW200A and the SA disagree by up to ~ppm "
                   "(several kHz at 5.6 GHz): set the offset above, and keep this wide enough for what is left."),
        Param("noise_inner_hz", "noise from", unit="kHz", scale=1e3, default=30e3, group=ANALYSIS,
              help="Noise = mean (in W) of the bins at least this far from the SA center, on both sides."),
        Param("noise_outer_hz", "noise to", unit="kHz", scale=1e3, default=None, group=ANALYSIS,
              help="... and at most this far. Empty = to the edge of the span."),
        # qubit
        Param("flux_mode", "qubit flux bias", kind="select", default="keep", group=QUBIT,
              options=[["keep", "leave it as it is"], ["set", "set it for the sweep"]],
              help="Either way it is reserved during the sweep and its state is saved."),
        Param("flux_bias_v", "flux bias", unit="V", default=0.0, group=QUBIT, show_if={"flux_mode": ["set"]},
              from_state=[FLUX, "level"],
              help="Limit: qubit_flux_bias level (VOLT mode) in the settings tab."),
        # before / after
        Param("set_switches", "set the warm switches to SMW200A probe -> fridge -> SA first", kind="bool",
              default=True, group=END,
              help="fridge_in -> rf_probe, rx_source -> fridge, receiver -> spa."),
        Param("finish", "when done", kind="select", default="restore", group=END,
              options=[["restore", "restore the SA, switches, attenuator, probe and flux bias as they were"],
                       ["last", "leave everything at the last point"],
                       ["off", "turn the probe off (and the flux bias, ramped to 0 V, if the sweep set it); "
                               "the SA, switches and attenuator stay"]]),
        # save
        Param("fridge_temperature_k", "fridge temperature (for the record)", unit="K", default=None, group=SAVE),
        Param("label", "label", kind="text", default="qubit-power-sweep-sa", group=SAVE),
        Param("note", "note", kind="text", default="", group=SAVE),
    ]

    # ---- settings ----
    @staticmethod
    def att_mode(p: dict) -> bool:
        return p["power_control"] == ATT

    @classmethod
    def sets_att(cls, p: dict) -> bool:
        """The run changes the attenuator (steps it, or sets it once)."""
        return cls.att_mode(p) or p["att_mode"] == "set"

    def y_axis(self, p: dict) -> tuple[str, str, str]:
        if self.att_mode(p):
            return "attenuation_db", "attenuator", "dB"
        return "probe_power_dbm", "SMW200A probe power", "dBm"

    def source_dbm(self, p: dict, value: float) -> float:
        """SMW200A probe power at the power point value."""
        return p["synth_power_dbm"] if self.att_mode(p) else value

    def drive_dbm(self, p: dict, value: float, att_db: float | None) -> float | None:
        """Power after the warm attenuator; att_db is its (fixed) value in synth mode."""
        if self.att_mode(p):
            return round(p["synth_power_dbm"] - value, 6)
        return None if att_db is None else round(value - att_db, 6)

    @staticmethod
    def noise_outer(p: dict) -> float:
        return p["noise_outer_hz"] if p["noise_outer_hz"] is not None else p["span_hz"] / 2

    # ---- plan ----
    def power_points(self, p: dict) -> tuple[list[tuple[float, int]], list[str], list[str]]:
        if self.att_mode(p):
            return segment_points(p["att_segments"], "start_db", "stop_db", self.cap("max_points"))
        return segment_points(p["segments"], max_points=self.cap("max_points"))

    def probe_freqs(self, p: dict) -> tuple[list[float], list[str], list[str]]:
        start, stop, step = p["freq_start_hz"], p["freq_stop_hz"], p["freq_step_hz"]
        if start == stop:
            return [start], [], []
        if step <= 0:
            return [], ["probe frequency step must be > 0"], []
        if abs(stop - start) / step + 1 > self.cap("max_points"):
            return [], [f"more than {self.cap('max_points')} probe frequencies; use a larger step"], []
        values, warn = sweep_values(start / 1e6, stop / 1e6, step / 1e6, "MHz")
        return [round(v * 1e6, 3) for v in values], [], [f"probe frequency: {warn}"] if warn else []

    def check_regions(self, p: dict, errors: list[str], warnings: list[str]):
        """The noise region and the tone window on the SA's bins (the same at every point)."""
        check_centered_regions(errors, warnings, p["span_hz"], p["noise_inner_hz"], self.noise_outer(p),
                               p["signal_window_hz"], p["rbw_hz"])
        check_center_offset(warnings, p["sa_offset_hz"], max(p["freq_start_hz"], p["freq_stop_hz"]))

    def plan(self, p: dict, mgr=None) -> dict:
        points, errors, warnings = self.power_points(p)
        freqs, ferr, fwarn = self.probe_freqs(p)
        errors, warnings, notes = errors + ferr, warnings + fwarn, []
        values = [v for v, _ in points]
        n_total = len(points) * len(freqs)
        if n_total > self.cap("max_total_points"):
            errors.append(f"{len(points)} powers x {len(freqs)} frequencies = {n_total} points, more than "
                          f"{self.cap('max_total_points')}; use larger steps")
        if freqs:
            self.check_limit(errors, PROBE, "probe_frequency_hz", [min(freqs), max(freqs)], "probe frequency", "Hz")
            if not all(PROBE_FREQ_HZ[0] <= f <= PROBE_FREQ_HZ[1] for f in (min(freqs), max(freqs))):
                errors.append("probe frequency must be within 0.0001..44 GHz (SMW200A)")
            off = p["sa_offset_hz"]
            self.check_limit(errors, "spa", "center_hz", [min(freqs) + off, max(freqs) + off], "SA frequency", "Hz")
        powers = [p["synth_power_dbm"]] if self.att_mode(p) else values
        if powers:
            self.check_limit(errors, PROBE, "probe_power_dbm", powers, "probe power", "dBm")
            if not all(PROBE_POWER_DBM[0] <= pw <= PROBE_POWER_DBM[1] for pw in powers):
                errors.append("probe power must be within -145..30 dBm (SMW200A)")
        atts = values if self.att_mode(p) else [p["attenuation_db"]] if p["att_mode"] == "set" else []
        if atts:
            self.check_limit(errors, ATT, "attenuation_db", atts, "attenuation", "dB")
            off_grid = sorted({v for v in atts if abs(v / ATT_STEP_DB - round(v / ATT_STEP_DB)) > 1e-9})
            if off_grid:
                errors.append(f"attenuation not a multiple of {ATT_STEP_DB:g} dB: "
                              + ", ".join(f"{v:g}" for v in off_grid[:5]))
        if p["flux_mode"] == "set":
            self.check_limit(errors, FLUX, "level", p["flux_bias_v"], "flux bias", "V")
        if p["rbw_hz"] <= 0 or p["vbw_hz"] <= 0:
            errors.append("RBW and VBW must be > 0")
        else:
            self.check_regions(p, errors, warnings)
        if p["sweep_time_s"] is not None and p["sweep_time_s"] <= 0:
            errors.append("sweep time must be > 0; leave it empty for the SA's auto sweep time")
        if p["settle_s"] < 0:
            errors.append("wait after each step must be >= 0")
        for key, value, what, unit in (("span_hz", p["span_hz"], "SA span", "Hz"),
                                       ("rbw_hz", p["rbw_hz"], "RBW", "Hz"), ("vbw_hz", p["vbw_hz"], "VBW", "Hz"),
                                       ("sweep_time_s", p["sweep_time_s"], "sweep time", "s"),
                                       ("ref_level_dbm", p["ref_level_dbm"], "SA reference level", "dBm")):
            if value is not None:
                self.check_limit(errors, "spa", key, value, what, unit)

        sa_avg, att_now = 1, None
        if mgr is not None:
            sa_avg, att_now = self.check_instruments(mgr, p, errors, warnings, notes)
        att_db = p["attenuation_db"] if p["att_mode"] == "set" else att_now
        drives = [self.drive_dbm(p, v, att_db) for v in values]
        if points and freqs:
            traces = sum(n for _, n in points) * len(freqs)
            notes.append(f"{len(points)} powers x {len(freqs)} probe frequencies "
                         f"({freqs[0] / 1e9:g} -> {freqs[-1] / 1e9:g} GHz) = {n_total} points, {traces} spectra in all")
            if p["sa_offset_hz"]:
                notes.append(f"SA center = probe frequency {p['sa_offset_hz'] / 1e3:+g} kHz")
            if None not in drives:
                text = f"drive after the attenuator {drives[0]:g} -> {drives[-1]:g} dBm"
                if p["line_atten_db"] is not None:
                    text += (f", about {drives[0] - p['line_atten_db']:g} -> {drives[-1] - p['line_atten_db']:g} "
                             f"dBm at the qubit")
                notes.append(text)
        sweep_s = p["sweep_time_s"]
        if sweep_s is None and p["rbw_hz"] > 0 and p["vbw_hz"] > 0 and p["span_hz"] > 0:
            sweep_s = estimate_sweep_s(p["span_hz"], p["rbw_hz"], p["vbw_hz"])
            notes.append(f"sweep time about {sweep_s:.2g} s per spectrum (rough; SA auto)")
        traces = sum(n for _, n in points) * len(freqs)
        estimate_s = (traces * ((sweep_s or 0) * sa_avg + TRACE_OVERHEAD_S)
                      + n_total * (max(p["settle_s"], 0) + STEP_OVERHEAD_S))
        size_mb = n_total * (6 + (N_POINTS if p["save_traces"] else 0)) * 26 / 1e6
        if size_mb > self.cap("big_file_mb"):
            warnings.append(f"the data file would be about {size_mb:.0f} MB; turn off saving every spectrum")
        key, label, unit = self.y_axis(p)
        rows = []
        for i, ((v, n), d) in enumerate(zip(points, drives)):
            rows.append({"i": i, key: v, "drive_dbm": d, "averages": n})
        return {"points": rows, "estimate_s": round(estimate_s, 1), "size_mb": round(size_mb, 2),
                "columns": [["i", "#", ""], [key, label, unit], ["drive_dbm", "drive", "dBm"],
                            ["averages", "traces per frequency", ""]],
                "errors": errors, "warnings": warnings, "notes": notes}

    def check_instruments(self, mgr, p, errors, warnings, notes) -> tuple[int, float | None]:
        """Reads only. Returns (sweeps per trace (the SA's own averaging), the attenuation now)."""
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
            notes.append(f"SMW200A probe is {now}: stepped over the sweep and on")
        except Exception as e:
            errors.append(f"could not read the probe (SMW200A): {e}")
        try:
            notes.append(f"TWPA pump is {pump_text(self.read_state(mgr, SYNTH))} (not changed)")
        except Exception as e:
            warnings.append(f"could not read the SynthHD: {e}")

        att = None
        try:
            att = self.read_state(mgr, ATT)["attenuation_db"]
            if self.att_mode(p):
                then = "restored after the sweep" if p["finish"] == "restore" else "not restored after the sweep"
                first = self.power_points(p)[0][:1]
                notes.append(f"attenuator is at {att:g} dB now"
                             + (f"; set to {first[0][0]:g} dB before the probe is turned on, {then}" if first else ""))
            else:
                notes.append(f"attenuator is at {att:g} dB" + (f": set to {p['attenuation_db']:g} dB"
                                                                if p["att_mode"] == "set" else " (not changed)"))
        except Exception as e:
            (errors if self.sets_att(p) else warnings).append(f"could not read the attenuator: {e}")

        try:
            fl = self.read_state(mgr, FLUX)
            if p["flux_mode"] == "set":
                _, err, note = flux_setup(fl, [p["flux_bias_v"]])
                if err:
                    errors.append(err)
                else:
                    notes.append(note)
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
        return sa_avg, att

    # ---- result ----
    def map_axis(self, p: dict, freqs) -> dict:
        key, label, unit = self.y_axis(p)
        return {"x": np.asarray(freqs, dtype=float).tolist(), "x_unit": "Hz", "y_key": key, "y_label": label,
                "y_unit": unit, "no_phase": True, "z_label": "tone - probe power [dB]",
                "z_label_att": "tone - drive [dB]"}

    def result_view(self, rec: dict) -> dict:
        p = self.record_settings(rec)
        key = self.y_axis(p)[0]
        return {"axis": self.map_axis(p, rec["frequency_hz"]),
                "rows": [power_row(key, v, n, peak, self.source_dbm(p, v))
                         for v, n, peak in zip(rec[key], rec["averages"], rec["peak_w"])]}

    # ---- run ----
    def run(self, p: dict, job) -> None:
        plan = self.plan(p)
        if plan["errors"]:
            raise ValueError("; ".join(plan["errors"]))
        points, _, _ = self.power_points(p)
        freqs, _, _ = self.probe_freqs(p)
        key = self.y_axis(p)[0]
        mgr, owner = job.mgr, job.owner
        job.progress("reserving instruments", points_total=len(points) * len(freqs),
                     traces_total=sum(n for _, n in points) * len(freqs))

        with mgr.claim(self.instruments, owner):
            snapshot = mgr.snapshot(owner=owner)
            for name in ("spa", PROBE) + ((ATT,) if self.sets_att(p) else ()) \
                    + ((FLUX,) if p["flux_mode"] == "set" else ()):
                if "error" in snapshot.get(name, {}):
                    raise RuntimeError(f"cannot read {name}: {snapshot[name]['error']}")
            rec = job.record
            rec.update(kind="experiment", experiment=self.id, params=self.recorded_params(p), note=p["note"],
                       sim=mgr.sim, fridge_temperature_k=p["fridge_temperature_k"],
                       frequency_hz=np.asarray(freqs, dtype=float), **{key: []}, drive_dbm=[], averages=[],
                       point_start_time=[], point_end_time=[], peak_w=[], peak_hz=[], signal_w=[], noise_w=[],
                       noise_std_w=[], instruments=snapshot, analysis_note=LOG_BIAS_NOTE)
            if p["line_atten_db"] is not None:
                rec.update(line_atten_db=p["line_atten_db"], qubit_power_dbm=[])
            if p["save_traces"]:
                rec["traces_w"] = []
            job.axis = self.map_axis(p, freqs)
            try:
                self._measure(p, job, points, freqs, snapshot)
            finally:
                self._finish(p, job, snapshot)

    def _step(self, p, job, value):
        if self.att_mode(p):
            job.mgr.apply(ATT, job.owner, attenuation_db=value)
        else:
            job.mgr.apply(PROBE, job.owner, probe_power_dbm=value)

    def _measure(self, p, job, points, freqs, snapshot):
        mgr, owner, rec = job.mgr, job.owner, job.record
        key, _, unit = self.y_axis(p)
        if p["set_switches"]:
            job.progress("setting the warm switches")
            mgr.apply("warm_switches", owner, **SWITCH_PATH)
        # the lowest drive before the probe goes on
        if self.att_mode(p):
            job.progress(f"setting the attenuator to {points[0][0]:g} dB")
            mgr.apply(ATT, owner, attenuation_db=points[0][0])
        elif p["att_mode"] == "set":
            job.progress(f"setting the attenuator to {p['attenuation_db']:g} dB")
            mgr.apply(ATT, owner, attenuation_db=p["attenuation_db"])
        if p["flux_mode"] == "set":
            flux_start(job, snapshot[FLUX], [p["flux_bias_v"]])
        for name, k in ((ATT, "attenuator_state"), (FLUX, "flux_bias_state")):
            try:
                rec[k] = mgr.get_state(name, owner)       # during the sweep
            except Exception as e:
                job.notes.append(f"could not read the {name}: {e}")
        att_db = rec.get("attenuator_state", {}).get("attenuation_db")
        if att_db is None and not self.att_mode(p):
            job.notes.append("attenuator unknown: drive_dbm not recorded")

        off = p["sa_offset_hz"]
        sa = self._setup_sa(p, job, freqs[0] + off)
        offsets = sa_freqs(0.0, sa["span_hz"])
        inner, outer, window = p["noise_inner_hz"], self.noise_outer(p), p["signal_window_hz"]
        rec.update(sa_bin_offset_hz=offsets, sa_center_offset_hz=off, noise_inner_hz=inner, noise_outer_hz=outer,
                   signal_window_hz=window)

        src0 = self.source_dbm(p, points[0][0])
        job.progress(f"turning the probe on at {freqs[0] / 1e9:g} GHz, {src0:g} dBm")
        mgr.apply(PROBE, owner, probe_frequency_hz=freqs[0], probe_power_dbm=src0)
        mgr.apply(PROBE, owner, **{PROBE_ON: True})
        job.sleep(p["settle_s"])
        rec["probe_state"] = mgr.get_state(PROBE, owner)

        clip_w, clipped = dbm_to_w(p["ref_level_dbm"]), []
        last_f = freqs[0]
        for i, (value, n) in enumerate(points):
            job.check()
            if i:
                self._step(p, job, value)
                job.sleep(p["settle_s"])
            line = {k: [] for k in ("peak_w", "peak_hz", "signal_w", "noise_w", "noise_std_w")}
            spectra = []
            t0 = time.time()
            for j, f in enumerate(freqs):
                job.check()
                job.progress(f"{value:g} {unit} ({i + 1}/{len(points)}), {f / 1e9:.6f} GHz, {n} traces",
                             points_done=i * len(freqs) + j)
                if f != last_f:
                    mgr.apply(PROBE, owner, probe_frequency_hz=f)
                    mgr.apply("spa", owner, center_hz=f + off)
                    last_f = f
                    job.sleep(p["settle_s"])
                bins = f + off + offsets
                p_w = self._spectrum(p, job, bins, n)
                a = analyze(bins, p_w, f + off, f + off, inner, outer, window)
                for k in line:
                    line[k].append(a[k])
                if p["save_traces"]:
                    spectra.append(p_w)
                if p_w.max() > clip_w:
                    clipped.append(f"{value:g} {unit} / {f / 1e9:g} GHz")
            t1 = time.time()
            drive = self.drive_dbm(p, value, att_db)
            rec[key].append(value)
            rec["drive_dbm"].append(drive)
            if "qubit_power_dbm" in rec:
                rec["qubit_power_dbm"].append(None if drive is None else round(drive - p["line_atten_db"], 6))
            rec["averages"].append(n)
            rec["point_start_time"].append(round(t0, 3))
            rec["point_end_time"].append(round(t1, 3))
            for k, v in line.items():
                rec[k].append(v)
            if p["save_traces"]:
                rec["traces_w"].append(spectra)
            job.add_row(power_row(key, value, n, line["peak_w"], self.source_dbm(p, value)))
        weak = sum(s is None for line in rec["signal_w"] for s in line)
        if weak:
            job.notes.append(f"the tone is not above the noise at {weak} of {len(points) * len(freqs)} points "
                             f"(signal_w null there; peak_w is still saved)")
        if clipped:
            job.notes.append(f"bins above the reference level ({p['ref_level_dbm']:g} dBm) at {len(clipped)} "
                             f"points, e.g. {clipped[0]}: the SA may have clipped; raise the reference level")
        job.progress(f"{len(points)} powers x {len(freqs)} frequencies measured",
                     points_done=len(points) * len(freqs))

    def _finish(self, p, job, snapshot):
        """Leave the instruments as asked. After "outputs off" nothing is sent: the
        outputs are already off and every command would be refused. On restore the
        attenuator is raised (if it was higher) before the probe is restored and lowered
        after, so the drive never exceeds both the sweep's and the old one."""
        mgr, owner = job.mgr, job.owner
        if mgr.halt.is_set():
            return
        sa0, sw0, probe0 = snapshot.get("spa", {}), snapshot.get("warm_switches", {}), snapshot.get(PROBE, {})
        att0 = snapshot.get(ATT, {})
        done = []
        try:
            if p["finish"] == "restore":
                old_att = None
                if self.sets_att(p) and "error" not in att0:
                    if within(self.limits(ATT).get("attenuation_db"), att0["attenuation_db"]):
                        old_att = att0["attenuation_db"]
                    else:
                        job.notes.append(f"attenuator not restored: the old value {att0['attenuation_db']:g} dB "
                                         f"is outside the limits")
                if old_att is not None and old_att > mgr.get_state(ATT, owner)["attenuation_db"]:
                    mgr.apply(ATT, owner, attenuation_db=old_att)
                if "error" not in probe0:
                    synth_restore(self, job, probe0, done, ch="probe")
                if old_att is not None:
                    mgr.apply(ATT, owner, attenuation_db=old_att)
                    done.append("attenuator restored")
                if p["flux_mode"] == "set":
                    flux_finish(job, snapshot.get(FLUX, {}), "restore", self.limits(FLUX), done)
                self._restore_sa(job, sa0, done)
                if p["set_switches"] and "error" not in sw0:
                    mgr.apply("warm_switches", owner, **{k: sw0[k] for k in SWITCH_PATH if k in sw0})
                    done.append("switches restored")
            elif p["finish"] == "off":
                mgr.apply(PROBE, owner, **{PROBE_ON: False})
                done.append("probe off")
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
