"""Qubit flux sweep (VNA S21 versus flux bias): loading, transmon fit, plots.

Conventions
-----------
* Frequencies are in GHz, flux bias is the Yokogawa GS210 output in V.
* The qubit shows up as a dip in |S21|. Its frequency at each flux point is
  the minimum of |S21 / S21_bg|, where the background ``S21_bg`` is the median
  over flux at each frequency (removes the line's standing waves as long as
  the qubit moves across the span).

The notebook ``qubit_flux_scan.ipynb`` uses these functions to make the two
figures of this folder: the gray |S21| map, and the same map with the
transmon fit.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import least_squares


# Charging energy E_C/h [GHz]. The transmon anharmonicity is alpha ~ -E_C, and
# the 3-level fit of the 5.58 GHz power sweep gives alpha/2pi = -223 MHz
# (../2_qubit_power_fit/3-level-fit/nl3_pm20.json).
EC_GHZ = 0.223


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@dataclass
class FluxSweep:
    """Averaged complex S21 versus flux bias and frequency."""
    fghz: np.ndarray        # (n_freq,)
    flux_v: np.ndarray      # (n_flux,)
    s21: np.ndarray         # complex, (n_flux, n_freq)
    meta: dict = field(default_factory=dict)
    path: Path | None = None

    @property
    def s21_db(self):
        return 20 * np.log10(np.abs(self.s21))


_RAW_KEYS = {"s_real", "s_imag", "fghz", "flux_bias", "averages"}


def load_sweep(path):
    """Load a ``qubit_flux_sweep`` JSON file from the lab server.

    The file holds ``s_real``, ``s_imag`` of shape (n_flux, n_freq), or
    (n_flux, n_avg, n_freq) for raw traces, which are then averaged.
    """
    path = Path(path)
    with open(path, "r") as f:
        d = json.load(f)
    s = np.asarray(d["s_real"], dtype=float) + 1j * np.asarray(d["s_imag"], dtype=float)
    if s.ndim == 3:
        s = s.mean(axis=1)
    meta = {k: v for k, v in d.items() if k not in _RAW_KEYS}
    return FluxSweep(fghz=np.asarray(d["fghz"], dtype=float),
                     flux_v=np.asarray(d["flux_bias"], dtype=float),
                     s21=s, meta=meta, path=path)


def title(sweep):
    """Plot title: file name without the Unix-time prefix, measurement date and VNA power."""
    m = sweep.meta
    name = sweep.path.stem.split("-", 1)[-1] if sweep.path else ""
    return (f"{name}, {m.get('date')} {m.get('time', '')[:5]}, "
            f"VNA {m.get('params', {}).get('power_dbm'):g} dBm")


def summary(sweep):
    """Short text description of a sweep."""
    m = sweep.meta
    p = m.get("params", {})
    att = m.get("instruments", {}).get("attenuator", {}).get("attenuation_db")
    return "\n".join([
        f"file        : {sweep.path}  ({m.get('date')} {m.get('time')})",
        f"frequency   : {sweep.fghz[0]:.4f} - {sweep.fghz[-1]:.4f} GHz  ({sweep.fghz.size} points)",
        f"flux bias   : {sweep.flux_v[0]:g} -> {sweep.flux_v[-1]:g} V  ({sweep.flux_v.size} points)",
        f"VNA power   : {p.get('power_dbm')} dBm, IF bw {p.get('ifbw_hz')} Hz, averages {p.get('averages')}",
        f"attenuator  : {att} dB (warm programmable)",
    ])


# ---------------------------------------------------------------------------
# Qubit frequency versus flux
# ---------------------------------------------------------------------------

def find_qubit_dips(sweep, smooth=7, edge_mhz=10.0):
    """Qubit frequency [GHz] at each flux point, from the minimum of |S21 / S21_bg|.

    ``smooth`` is a moving average over that many frequency points before
    taking the minimum. Flux points whose minimum lies within ``edge_mhz`` of
    the span edges (qubit outside the span) are dropped.
    Returns ``(flux_v, f_ghz)``.
    """
    bg = np.median(sweep.s21.real, axis=0) + 1j * np.median(sweep.s21.imag, axis=0)
    t = np.abs(sweep.s21 / bg)
    kernel = np.ones(smooth) / smooth
    t = np.array([np.convolve(row, kernel, mode="valid") for row in t])
    f = sweep.fghz[smooth // 2: smooth // 2 + t.shape[1]][np.argmin(t, axis=1)]
    keep = (f > sweep.fghz[0] + edge_mhz / 1e3) & (f < sweep.fghz[-1] - edge_mhz / 1e3)
    return sweep.flux_v[keep], f[keep]


def transmon_f01(flux_v, f_max, d, v0, period, ec=EC_GHZ):
    """f01 [GHz] of an asymmetric-SQUID transmon (Koch et al., PRA 76, 042319).

    E_J(phi) = E_J,max sqrt(cos^2 phi + d^2 sin^2 phi),  phi = pi (V - v0) / period,
    f01 = sqrt(8 E_J E_C) - E_C, written with f_max = f01 at the upper sweet
    spot ``v0``. The lower sweet spot is at ``v0 +- period / 2``.
    """
    phi = np.pi * (np.asarray(flux_v) - v0) / period
    return (f_max + ec) * (np.cos(phi) ** 2 + d ** 2 * np.sin(phi) ** 2) ** 0.25 - ec


@dataclass
class TransmonFit:
    """Result of :func:`fit_transmon`."""
    names = ("f_max", "d", "v0", "period")
    params: np.ndarray
    errors: np.ndarray
    ec: float
    flux_v: np.ndarray      # all points given to the fit
    f_ghz: np.ndarray
    inlier: np.ndarray      # bool, points kept in the final fit

    def __call__(self, flux_v):
        return transmon_f01(flux_v, *self.params, ec=self.ec)

    @property
    def v_min(self):
        """Lower sweet spot [V] nearest to the data."""
        v0, per = self.params[2], self.params[3]
        return min((v0 - per / 2, v0 + per / 2), key=lambda v: abs(v - np.mean(self.flux_v)))

    @property
    def f_min(self):
        f_max, d = self.params[:2]
        return (f_max + self.ec) * np.sqrt(d) - self.ec

    def report(self):
        units = {"f_max": "GHz", "d": "", "v0": "V", "period": "V"}
        res = self(self.flux_v[self.inlier]) - self.f_ghz[self.inlier]
        lines = [f"{n:7s} = {p:.4f} +- {e:.4f} {units[n]}"
                 for n, p, e in zip(self.names, self.params, self.errors)]
        lines += [f"E_C     = {self.ec:.3f} GHz (fixed)",
                  f"f_min   = {self.f_min:.4f} GHz at {self.v_min:.4f} V",
                  f"points  : {self.inlier.sum()} of {self.inlier.size} used, "
                  f"rms residual {1e3 * np.sqrt(np.mean(res ** 2)):.1f} MHz"]
        return "\n".join(lines)


def fit_transmon(flux_v, f_ghz, p0=(8.5, 0.4, 0.3, 1.2), ec=EC_GHZ, outlier_mhz=30.0):
    """Fit :func:`transmon_f01` to qubit frequencies versus flux.

    ``p0`` is ``(f_max, d, v0, period)``. A first robust fit (soft L1 loss)
    flags points further than ``outlier_mhz`` from the curve (anti-crossings,
    noise); the final fit is plain least squares on the remaining points and
    gives the 1-sigma errors.
    """
    flux_v, f_ghz = np.asarray(flux_v), np.asarray(f_ghz)
    bounds = ([0.0, 0.0, -np.inf, 0.0], [np.inf, 1.0, np.inf, np.inf])

    def residual(p, v, f):
        return transmon_f01(v, *p, ec=ec) - f

    r = least_squares(residual, p0, args=(flux_v, f_ghz), bounds=bounds,
                      loss="soft_l1", f_scale=0.02)
    inlier = np.abs(r.fun) < outlier_mhz / 1e3
    r = least_squares(residual, r.x, args=(flux_v[inlier], f_ghz[inlier]), bounds=bounds)
    dof = inlier.sum() - r.x.size
    cov = np.linalg.pinv(r.jac.T @ r.jac) * np.sum(r.fun ** 2) / dof
    return TransmonFit(params=r.x, errors=np.sqrt(np.diag(cov)), ec=ec,
                       flux_v=flux_v, f_ghz=f_ghz, inlier=inlier)


# ---------------------------------------------------------------------------
# Plots (each returns the Figure so the notebook can save it)
# ---------------------------------------------------------------------------

def plot_s21_map(sweeps, ax=None, clip=(0.5, 99.5)):
    """Gray map of |S21| [dB] versus frequency and flux bias.

    ``sweeps`` is one :class:`FluxSweep` or a list; later sweeps are drawn on
    top of earlier ones, all with the same color scale (``clip`` percentiles).
    """
    sweeps = [sweeps] if isinstance(sweeps, FluxSweep) else list(sweeps)
    fig, ax = (ax.figure, ax) if ax else plt.subplots(figsize=(8, 6))
    lo, hi = np.nanpercentile(np.concatenate([s.s21_db.ravel() for s in sweeps]), clip)
    for s in sweeps:
        m = ax.pcolormesh(s.fghz, s.flux_v, s.s21_db, shading="nearest", rasterized=True,
                          cmap="Greys_r", vmin=lo, vmax=hi)
    fig.colorbar(m, ax=ax, label=r"$|S_{21}|$ [dB]")
    ax.set_xlim(min(s.fghz.min() for s in sweeps), max(s.fghz.max() for s in sweeps))
    ax.set_ylim(min(s.flux_v.min() for s in sweeps), max(s.flux_v.max() for s in sweeps))
    ax.set_xlabel("Frequency [GHz]")
    ax.set_ylabel("Flux bias [V]")
    return fig


def plot_fit(sweeps, fit, ax=None):
    """:func:`plot_s21_map` with the fitted transmon curve and its parameters."""
    fig = plot_s21_map(sweeps, ax=ax)
    ax = fig.axes[0]
    v = np.linspace(*ax.get_ylim(), 1000)
    ax.plot(fit(v), v, "r--", lw=1.2, label="transmon fit")
    f_max, d, v0, per = fit.params
    ax.text(0.98, 0.5,
            f"$f_\\max$ = {f_max:.3f} GHz\n$d$ = {d:.3f}\n"
            f"$V_0$ = {v0:.3f} V, period = {per:.3f} V\n"
            f"$f_\\min$ = {fit.f_min:.3f} GHz at {fit.v_min:.3f} V\n"
            f"$E_C$ = {fit.ec:.3f} GHz (fixed)",
            transform=ax.transAxes, ha="right", va="center", fontsize=9,
            bbox=dict(facecolor="white", alpha=0.85, edgecolor="none"))
    ax.legend(loc="upper left", fontsize=9)
    return fig
