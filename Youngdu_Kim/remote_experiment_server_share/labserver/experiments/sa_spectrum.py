"""Shared part of experiments that read a tone from SA spectra (not zero span): the
SMW200A probe through the fridge, an averaged spectrum on the E7405A, and the tone's
peak and the noise around it (TWPA pump sweep, qubit power sweep on the SA).

From each spectrum (traces averaged in W, sample detector, LOG scale):
  * noise  = mean of the bins with inner <= |f - f_SA| <= outer (both sides)
  * signal = max of the bins within +-window of the signal frequency, minus the noise
             (the raw max is peak_w)

With VBW << RBW the SA's video filter averages the log of the noise, so the noise reads
about 2.5 dB low while the tone does not (LOG_BIAS_NOTE).
"""
from __future__ import annotations

import math

import numpy as np

from ..drivers.e7405a import N_POINTS, dbm_to_w
from .y_factor import SA_KEYS

MIN_NOISE_BINS = 10         # per side, for a noise estimate
LOG_BIAS_NOTE = ("noise is read with VBW << RBW on a LOG scale, so it reads about 2.5 dB low and the tone "
                 "does not: it cancels in snri_db and noise_rise_db, not in snr_db or the peak - noise "
                 "subtraction")


def sa_freqs(center_hz: float, span_hz: float) -> np.ndarray:
    """The SA's bin frequencies (401 points across the span)."""
    return np.linspace(center_hz - span_hz / 2, center_hz + span_hz / 2, N_POINTS)


def regions(freqs, f_sa: float, f_signal: float, inner: float, outer: float, window: float
            ) -> tuple[np.ndarray, np.ndarray]:
    """(noise mask, signal mask). The signal mask is the bins within +-window of f_signal
    (the probe or the SA frequency), or the nearest bin if the window is narrower than the
    bin spacing."""
    freqs = np.asarray(freqs, dtype=float)
    off = np.abs(freqs - f_sa)
    noise = (off >= inner) & (off <= outer)
    signal = np.abs(freqs - f_signal) <= window
    if not signal.any():
        signal[np.argmin(np.abs(freqs - f_signal))] = True
    return noise, signal


def analyze(freqs, p_w, f_sa: float, f_signal: float, inner: float, outer: float, window: float) -> dict:
    """Signal and noise of one spectrum (in W per bin). signal_w is None when the peak is
    not above the noise."""
    freqs, p_w = np.asarray(freqs, dtype=float), np.asarray(p_w, dtype=float)
    noise, signal = regions(freqs, f_sa, f_signal, inner, outer, window)
    if not noise.any():
        raise ValueError("no bins in the noise region")
    k = np.flatnonzero(signal)[np.argmax(p_w[signal])]
    peak, n = float(p_w[k]), float(p_w[noise].mean())
    return {"peak_w": peak, "peak_hz": float(freqs[k]), "noise_w": n, "noise_std_w": float(p_w[noise].std()),
            "n_noise": int(noise.sum()), "signal_w": peak - n if peak > n else None}


def ratio_db(a, b) -> float | None:
    """10 log10(a / b), None if either is missing or not > 0."""
    if a is None or b is None or not (a > 0 and b > 0):
        return None
    return round(10 * math.log10(a / b), 4)


def w_to_dbm(w) -> float | None:
    return None if w is None or not w > 0 else round(10 * math.log10(w / 1e-3), 4)


def trace_dbm(p_w) -> list[float]:
    """A spectrum in W per bin as dBm, for the plot."""
    return np.round(10 * np.log10(np.maximum(np.asarray(p_w, dtype=float), 1e-30) / 1e-3), 2).tolist()


def check_centered_regions(errors: list[str], warnings: list[str], span_hz: float, inner: float, outer: float,
                           window: float, rbw_hz: float, what: str = "tone window"):
    """The noise region and the window on the SA's bins, for a tone at the SA center (the
    same at every point when the center follows the tone)."""
    if span_hz <= 0:
        errors.append("span must be > 0 (this takes a spectrum, not zero span)")
        return
    half = span_hz / 2
    if inner < 0 or outer <= inner:
        errors.append("noise region: 'noise from' must be >= 0 and below 'noise to'")
        return
    if outer > half * (1 + 1e-9):
        errors.append(f"noise region: 'noise to' {outer / 1e3:g} kHz is beyond the edge of the span "
                      f"({half / 1e3:g} kHz from the center)")
        return
    if window <= 0:
        errors.append(f"{what} must be > 0")
        return
    freqs = sa_freqs(0.0, span_hz)
    noise, signal = regions(freqs, 0.0, 0.0, inner, outer, window)
    per_side = min(int((noise & (freqs < 0)).sum()), int((noise & (freqs > 0)).sum()))
    if per_side < MIN_NOISE_BINS:
        errors.append(f"noise region has {per_side} bins on one side (at least {MIN_NOISE_BINS}); "
                      f"widen it or the span")
    if (noise & signal).any():
        errors.append(f"the {what} reaches into the noise region; make it narrower or move 'noise from' out")
    elif inner < window + 3 * rbw_hz:
        warnings.append(f"the noise region starts within 3 RBW of the {what}: the tone's skirt may raise "
                        f"the noise")
    bin_hz = span_hz / (N_POINTS - 1)
    if window < bin_hz / 2:
        warnings.append(f"{what} +-{window / 1e3:g} kHz is narrower than the bin spacing "
                        f"({bin_hz / 1e3:g} kHz): the nearest bin is used")
    if bin_hz > rbw_hz:
        warnings.append(f"bin spacing {bin_hz / 1e3:g} kHz is wider than the RBW: the bins may miss the top "
                        f"of the tone; use a narrower span or a wider RBW")


def check_center_offset(warnings: list[str], offset_hz: float, f_max: float, what: str = "probe frequency"):
    """Warns when the SA center offset is too large for two unlocked clocks (a few ppm)."""
    if f_max > 0 and abs(offset_hz) > 20e-6 * f_max:
        warnings.append(f"SA center offset {offset_hz / 1e3:g} kHz is {abs(offset_hz) / f_max * 1e6:.0f} ppm of "
                        f"the {what}, large for two unlocked clocks (a few ppm); check its sign and unit")


def estimate_sweep_s(span_hz: float, rbw_hz: float, vbw_hz: float) -> float:
    """Rough ESA auto sweep time (the run reads back what the SA takes)."""
    return max(0.005, 2.5 * span_hz / (rbw_hz * min(rbw_hz, vbw_hz)))


class SaSpectrumMixin:
    """SA setup, averaged spectra and the SA restore, for an Experiment whose params have
    rbw_hz, vbw_hz, span_hz, sweep_time_s and ref_level_dbm."""

    @staticmethod
    def read_state(mgr, name: str) -> dict:
        owner = mgr.slots[name].claimed_by or "plan"     # a running sweep holds it
        with mgr.use(name, owner=owner, timeout=2.0) as inst:
            return inst.get_state()

    def _spectrum(self, p, job, freqs, n: int) -> np.ndarray:
        """Mean of n spectra in W."""
        mgr, owner = job.mgr, job.owner
        total = np.zeros(len(freqs))
        for _ in range(n):
            job.check()
            with mgr.use("spa", owner) as spa:
                tr = spa.read_trace()
            y = np.asarray(tr.y, dtype=float)
            total += dbm_to_w(y) if str(tr.y_unit).upper() == "DBM" else y
            job.progress(traces_done=job.traces_done + 1)
        return total / n

    def _setup_sa(self, p, job, center_hz: float, detector: str = "SAMP") -> dict:
        """detector "AVER" also sets power averaging (:AVER:TYPE POW) and checks both read back."""
        mgr, owner, rec = job.mgr, job.owner, job.record
        job.progress("setting up the SA")
        # the scale goes before the unit (the E7405A changes the unit with it), the
        # reference level after the unit; the SA picks its own RBW/VBW steps, read back
        extra = {"average_type": "POW"} if detector == "AVER" else {}
        sa = mgr.apply("spa", owner, detector=detector, vbw_hz=p["vbw_hz"], rbw_hz=p["rbw_hz"],
                       center_hz=center_hz, span_hz=p["span_hz"], scale="LOG", unit="DBM", **extra)
        if str(sa["detector"]).upper() != detector:
            raise RuntimeError(f"SA detector is {sa['detector']}, not {detector}")
        if extra and not str(sa["average_type"]).upper().startswith("POW"):
            raise RuntimeError(f"SA average type is {sa['average_type']}, not POW")
        sa = mgr.apply("spa", owner, sweep_time_s=p["sweep_time_s"], ref_level=p["ref_level_dbm"])
        for key, want, label in (("rbw_hz", p["rbw_hz"], "RBW"), ("vbw_hz", p["vbw_hz"], "VBW")):
            if abs(sa[key] - want) > 1e-6 * want:
                job.notes.append(f"the SA took {label} {sa[key] / 1e3:g} kHz (asked for {want / 1e3:g} kHz)")
        if abs(sa["span_hz"] - p["span_hz"]) > 1e-6 * p["span_hz"]:
            raise RuntimeError(f"SA span is {sa['span_hz']:g} Hz, asked for {p['span_hz']:g} Hz")
        if str(sa["unit"]).upper() != "DBM" or sa.get("scale") != "LOG":
            raise RuntimeError(f"SA is in {sa.get('scale')} / {sa['unit']}, not LOG / DBM")
        rec["spa_setup"] = sa
        rec.update(rbw_hz=sa["rbw_hz"], vbw_hz=sa["vbw_hz"], sweep_time_s=sa["sweep_time_s"])
        return sa

    @staticmethod
    def _restore_sa(job, sa0: dict, done: list[str]):
        """The SA back to its settings in the snapshot; RBW/VBW auto coupling is not restored."""
        restore = {k: sa0[k] for k in SA_KEYS if k in sa0}
        if sa0.get("sweep_time_auto"):
            restore["sweep_time_s"] = None
        ref = restore.pop("ref_level", None)
        job.mgr.apply("spa", job.owner, **restore)
        if ref is not None:
            job.mgr.apply("spa", job.owner, ref_level=ref)      # in the old unit, set above
        done.append("SA restored")
