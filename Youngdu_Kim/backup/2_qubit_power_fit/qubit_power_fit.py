"""Fitting utilities for waveguide-qubit VNA power sweeps.

Models and power calibration follow
  [HD20] Hönigl-Decrinis et al., Phys. Rev. Applied 13, 024066 (2020)
         https://doi.org/10.1103/PhysRevApplied.13.024066
         Lorentzian model fitted to Re r (``lorentzian_fit_model``).
  [SC25] Sandbo Chang et al., arXiv:2503.07559, Appendix G, eq. (G.1)
         Fano phase theta_F, attenuation as a fit parameter, complex fit
         (``fano_fit_model``).

Conventions
-----------
* Frequencies are in GHz, rates (Gamma1, Gamma2, Gamma_phi, Omega) are in MHz
  and mean rate / (2 pi), drive powers are in dBm at the VNA output port.
* ``r = 1 - t`` where ``t`` is the transmission normalized by a reference
  (qubit detuned, or qubit saturated at high power).
* Models use Im r > 0 above resonance. Our VNA data has the opposite sign,
  so complex fits use ``np.conj(r)``.

The notebooks ``qubit_power_fit.ipynb`` and ``qubit_power_fit_fano.ipynb``
use these functions to look at the data and save results. New line shapes
should be added as another ``*_fit_model`` function with the same call
signature and passed to :func:`fit_global`.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit

H_PLANCK = 6.62607015e-34  # J s


# ---------------------------------------------------------------------------
# Data loading / normalization
# ---------------------------------------------------------------------------

@dataclass
class PowerSweep:
    """Averaged complex S21 versus VNA power and frequency."""
    fghz: np.ndarray        # (n_freq,)
    power_dbm: np.ndarray   # (n_power,)
    s21: np.ndarray         # complex, (n_power, n_freq)
    averages: np.ndarray    # (n_power,)
    meta: dict = field(default_factory=dict)
    path: Path | None = None

    @property
    def attenuator_db(self):
        """Warm programmable attenuator setting stored in the file, if any."""
        if "programmable_attenuator" in self.meta:
            return float(self.meta["programmable_attenuator"])
        att = self.meta.get("instruments", {}).get("attenuator", {})
        return float(att["attenuation_db"]) if "attenuation_db" in att else None


_RAW_KEYS = {"s_real", "s_imag", "traces_real", "traces_imag",
             "fghz", "frequency_hz", "power", "power_dbm", "averages"}


def load_sweep(path):
    """Load a VNA power sweep JSON file.

    Supports these file formats in this repo:
      * ``qubit_power_sweep`` (Oct 2026): ``power_dbm``, averaged ``s_real``
        of shape (n_power, n_freq) and per-power ``averages``.
      * ``vna-power-sweep`` (Sep 2026): ``power`` and raw ``s_real`` of shape
        (n_power, n_avg, n_freq), averaged here over axis 1.
      * ``live_trace`` (e.g. ``no-qubit-ref.json``): a single VNA trace under
        ``trace`` with ``x`` [Hz], ``y_real``, ``y_imag``. Loaded as a sweep
        with one power.
    """
    path = Path(path)
    with open(path, "r") as f:
        d = json.load(f)

    if "trace" in d and "y_real" in d["trace"]:
        tr = d["trace"]
        s = np.asarray(tr["y_real"], dtype=float) + 1j * np.asarray(tr["y_imag"], dtype=float)
        meta = {k: v for k, v in d.items() if k != "trace"}
        meta["trace"] = {k: v for k, v in tr.items() if k not in ("x", "y_real", "y_imag")}
        return PowerSweep(fghz=np.asarray(tr["x"], dtype=float) / 1e9,
                          power_dbm=np.array([float(tr.get("power_dbm", np.nan))]),
                          s21=s[None, :],
                          averages=np.array([int(tr.get("averages", 1))]),
                          meta=meta, path=path)

    fghz = np.asarray(d["fghz"], dtype=float)
    s = np.asarray(d["s_real"], dtype=float) + 1j * np.asarray(d["s_imag"], dtype=float)

    if s.ndim == 3:  # raw traces: (n_power, n_avg, n_freq)
        averages = np.full(s.shape[0], s.shape[1])
        s = s.mean(axis=1)
    else:
        averages = np.asarray(d.get("averages", np.ones(s.shape[0])), dtype=int)

    power = np.asarray(d["power_dbm"] if "power_dbm" in d else d["power"], dtype=float)
    meta = {k: v for k, v in d.items() if k not in _RAW_KEYS}
    return PowerSweep(fghz=fghz, power_dbm=power, s21=s, averages=averages,
                      meta=meta, path=path)


def reference_trace(sweep, power_dbm=None):
    """Return one trace of ``sweep`` to use as a normalization reference.

    Default is the highest-power trace, where the qubit is saturated and
    nearly transparent. Pass the same power as ``p_norm`` to the fit so the
    small residual reflection of that trace is included in the model.
    """
    if power_dbm is None:
        i = int(np.argmax(sweep.power_dbm))
    else:
        i = int(np.argmin(np.abs(sweep.power_dbm - power_dbm)))
    return sweep.s21[i], float(sweep.power_dbm[i])


def normalize(sweep, reference):
    """Normalized transmission ``t = S21 / S21_ref``.

    ``reference`` may be
      * a 1D complex array (one reference trace, broadcast over powers), or
      * a :class:`PowerSweep` taken with the qubit detuned. If its powers
        match ``sweep`` it is divided power by power (as in the original
        notebook); otherwise its traces are averaged first. A different
        frequency grid is interpolated onto ``sweep.fghz``.
    """
    if isinstance(reference, PowerSweep):
        ref = reference.s21
        if not (ref.shape[0] == sweep.s21.shape[0]
                and np.allclose(reference.power_dbm, sweep.power_dbm)):
            ref = ref.mean(axis=0)
        if not (reference.fghz.shape == sweep.fghz.shape
                and np.allclose(reference.fghz, sweep.fghz)):
            ref = _interp_complex(sweep.fghz, reference.fghz, ref)
    else:
        ref = np.asarray(reference)
    return sweep.s21 / ref


def _interp_complex(x, xp, fp):
    fp = np.atleast_2d(fp)
    out = np.array([np.interp(x, xp, row.real) + 1j * np.interp(x, xp, row.imag)
                    for row in fp])
    return out[0] if out.shape[0] == 1 else out


def trace_noise(y):
    """Robust per-trace noise estimate along the last axis.

    Uses the second difference, which removes the smooth line shape, and the
    median absolute deviation, which ignores a few outliers. For complex
    input this is the rms of the real and imaginary noise.
    """
    y = np.asarray(y)
    if np.iscomplexobj(y):
        return np.sqrt((trace_noise(y.real) ** 2 + trace_noise(y.imag) ** 2) / 2)
    d2 = np.diff(y, n=2, axis=-1)
    mad = np.median(np.abs(d2 - np.median(d2, axis=-1, keepdims=True)), axis=-1)
    return 1.4826 * mad / np.sqrt(6.0)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

def reflection_model(f, f0, g1, g2, omega):
    """Complex reflection of a driven two-level system in a waveguide (eq. 3).

    f, f0 in GHz; g1, g2, omega in MHz (rate / 2 pi).
    """
    df = (f - f0) * 1000.0  # MHz
    a = g1 / (2 * g2)
    b = 1 + 1j * df / g2
    c = 1 + (df / g2) ** 2 + omega ** 2 / (g1 * g2)
    return a * b / c


def omega_at(p_dbm, omega_ref, p_ref):
    """Rabi frequency at VNA power ``p_dbm`` given ``omega_ref`` at ``p_ref``."""
    return omega_ref * 10 ** ((np.asarray(p_dbm) - p_ref) / 20)


def lorentzian_fit_model(x, f0, g1, g2, omega_ref, background, *, p_ref, p_norm=None):
    """Re r for all powers at once (original notebook model).

    x = (f_ghz, p_dbm) flattened. ``background`` is a constant offset on Re r
    that absorbs imperfect normalization. If the data were normalized by the
    trace measured at ``p_norm`` (saturated qubit), the model is normalized
    the same way: r = 1 - (1 - r(P)) / (1 - r(P_norm)).
    """
    f, p = x
    r = reflection_model(f, f0, g1, g2, omega_at(p, omega_ref, p_ref))
    if p_norm is not None:
        r_norm = reflection_model(f, f0, g1, g2, omega_at(p_norm, omega_ref, p_ref))
        r = 1 - (1 - r) / (1 - r_norm)
    return r.real + background


LORENTZIAN = dict(
    model=lorentzian_fit_model,
    names=["f0", "g1", "g2", "omega_ref", "background"],
    units=["GHz", "MHz", "MHz", "MHz", ""],
)


def omega_from_power(p_qubit_dbm, g1, f0):
    """Rabi frequency [MHz] for power ``p_qubit_dbm`` at the qubit.

    P = hbar w_q Omega^2 / (2 Gamma1)  [SC25]  ->  Omega = sqrt(2 Gamma1 P / (h f0)).
    """
    gamma1 = 2 * np.pi * g1 * 1e6
    p_w = 1e-3 * 10 ** (np.asarray(p_qubit_dbm) / 10)
    return np.sqrt(2 * gamma1 * p_w / (H_PLANCK * f0 * 1e9)) / (2 * np.pi * 1e6)


def fano_reflection_model(f, f0, g1, gphi, omega, theta_f, xi=1.0):
    """Complex r with Fano phase, eq. (G.1) of [SC25] written as r = 1 - t.

    Gamma2 = Gamma1 / 2 + Gamma_phi; ``xi`` is the emission ratio into the line.
    """
    return xi * np.exp(1j * theta_f) * reflection_model(f, f0, g1, g1 / 2 + gphi, omega)


def fano_fit_model(x, f0, g1, gphi, att_db, theta_f, bg_re, bg_im, *, p_norm=None, xi=1.0):
    """Complex r for all powers at once [SC25].

    The drive at the qubit is the VNA power minus ``att_db`` (A_sys), so the
    attenuation is a direct fit parameter. ``bg_re + i bg_im`` is a constant
    complex background; ``p_norm`` as in :func:`lorentzian_fit_model`.
    """
    f, p = x

    def r_at(p_dbm):
        omega = omega_from_power(p_dbm - att_db, g1, f0)
        return fano_reflection_model(f, f0, g1, gphi, omega, theta_f, xi)

    r = r_at(p)
    if p_norm is not None:
        r = 1 - (1 - r) / (1 - r_at(p_norm))
    return r + bg_re + 1j * bg_im


FANO = dict(
    model=fano_fit_model,
    names=["f0", "g1", "gphi", "att_db", "theta_f", "bg_re", "bg_im"],
    units=["GHz", "MHz", "MHz", "dB", "rad", "", ""],
)


# ---------------------------------------------------------------------------
# Global fit
# ---------------------------------------------------------------------------

@dataclass
class FitResult:
    names: list
    units: list
    popt: np.ndarray        # all parameters, fixed ones included
    perr: np.ndarray        # 0 for fixed parameters
    pcov: np.ndarray        # free parameters only
    model: object
    model_kwargs: dict
    fixed: dict
    f_range: tuple
    power_range: tuple
    fmask: np.ndarray
    pmask: np.ndarray
    is_complex: bool
    rms: float              # rms of (data - model), real and imag stacked
    chi2_red: float | None  # only when sigma is given

    def __getitem__(self, name):
        return self.popt[self.names.index(name)]

    def err(self, name):
        return self.perr[self.names.index(name)]

    @property
    def params(self):
        return dict(zip(self.names, self.popt))

    def evaluate(self, f, p):
        """Model r (Re r for real models) at ``f`` (GHz) and scalar or array power."""
        f = np.asarray(f, dtype=float)
        p = np.broadcast_to(np.asarray(p, dtype=float), f.shape)
        return self.model((f, p), *self.popt, **self.model_kwargs)

    def report(self):
        lines = []
        for n, u, v, e in zip(self.names, self.units, self.popt, self.perr):
            tag = " (fixed)" if n in self.fixed else ""
            lines.append(f"{n:>12} = {v:.7g} ± {e:.2g} {u}".rstrip() + tag)
        lines.append(f"{'rms resid':>12} = {self.rms:.3g}")
        if self.chi2_red is not None:
            lines.append(f"{'chi2_red':>12} = {self.chi2_red:.3g}")
        return "\n".join(lines)

    def to_dict(self):
        return {
            "params": {n: float(v) for n, v in zip(self.names, self.popt)},
            "errors": {n: float(e) for n, e in zip(self.names, self.perr)},
            "units": dict(zip(self.names, self.units)),
            "fixed": {k: float(v) for k, v in self.fixed.items()},
            "model": self.model.__name__,
            "model_kwargs": self.model_kwargs,
            "complex": self.is_complex,
            "f_range_ghz": list(self.f_range),
            "power_range_dbm": list(self.power_range),
            "rms": float(self.rms),
            "chi2_red": None if self.chi2_red is None else float(self.chi2_red),
        }


def fit_global(fghz, power_dbm, y, *, model, names, p0, bounds,
               units=None, f_range=None, power_range=None, sigma=None,
               fixed=None, model_kwargs=None, max_nfev=30000):
    """Fit ``model`` to all selected powers simultaneously.

    model(x, *params, **model_kwargs) with x = (f_ghz, p_dbm) flattened.
    If ``y`` is complex the model must return complex values and real and
    imaginary parts are fitted together; otherwise ``y`` is e.g. Re r.
    ``p0`` and ``bounds`` cover all ``names``; parameters in ``fixed``
    ({name: value}) are held at that value. ``sigma`` is an optional
    per-power noise (n_power,) used as relative weights; parameter errors
    are always scaled by the reduced chi^2.
    """
    fghz = np.asarray(fghz)
    power_dbm = np.asarray(power_dbm)
    y_all = np.asarray(y)
    is_complex = np.iscomplexobj(y_all)
    model_kwargs = dict(model_kwargs or {})
    fixed = dict(fixed or {})
    f_range = f_range or (fghz.min(), fghz.max())
    power_range = power_range or (power_dbm.min(), power_dbm.max())

    fmask = (fghz >= f_range[0]) & (fghz <= f_range[1])
    pmask = (power_dbm >= power_range[0]) & (power_dbm <= power_range[1])
    if sigma is not None:
        sigma = np.asarray(sigma, dtype=float)
        pmask &= sigma > 0  # e.g. the reference trace itself (r == 0 exactly)
    F, P = np.meshgrid(fghz[fmask], power_dbm[pmask])
    x = (F.ravel(), P.ravel())
    yd = y_all[np.ix_(pmask, fmask)].ravel()
    sig = None
    if sigma is not None:
        sig = np.broadcast_to(sigma[pmask][:, None], F.shape).ravel()
        if is_complex:
            sig = np.concatenate([sig, sig])

    free = [i for i, n in enumerate(names) if n not in fixed]

    def full(pfree):
        p = np.array([fixed.get(n, 0.0) for n in names], dtype=float)
        p[free] = pfree
        return p

    def stack(z):
        return np.concatenate([z.real, z.imag]) if is_complex else z

    def f(x, *pfree):
        return stack(model(x, *full(pfree), **model_kwargs))

    popt_free, pcov = curve_fit(
        f, x, stack(yd),
        p0=np.asarray(p0, dtype=float)[free],
        bounds=(np.asarray(bounds[0], dtype=float)[free],
                np.asarray(bounds[1], dtype=float)[free]),
        sigma=sig,
        x_scale="jac", max_nfev=max_nfev,
    )
    resid = stack(yd) - f(x, *popt_free)
    rms = float(np.sqrt(np.mean(resid ** 2)))
    chi2_red = None
    if sig is not None:
        chi2_red = float(np.sum((resid / sig) ** 2) / max(resid.size - len(free), 1))

    perr = np.zeros(len(names))
    perr[free] = np.sqrt(np.diag(pcov))
    return FitResult(
        names=list(names), units=list(units or [""] * len(names)),
        popt=full(popt_free), perr=perr, pcov=pcov,
        model=model, model_kwargs=model_kwargs, fixed=fixed,
        f_range=tuple(float(v) for v in f_range),
        power_range=tuple(float(v) for v in power_range),
        fmask=fmask, pmask=pmask, is_complex=is_complex,
        rms=rms, chi2_red=chi2_red,
    )


def fit_power_sweep(fghz, power_dbm, re_r, *, p_ref=-30.0, p_norm=None,
                    f_range=None, power_range=None, sigma=None,
                    p0=None, bounds=None):
    """Global Lorentzian fit with the original notebook's defaults."""
    f_range = f_range or (fghz.min(), fghz.max())
    if p0 is None:
        p0 = [0.5 * (f_range[0] + f_range[1]), 20.0, 10.0, 9.0, 0.0]
    if bounds is None:
        bounds = ([f_range[0], 1.0, 1.0, 0.0, -0.3],
                  [f_range[1], 100.0, 100.0, 200.0, 0.3])
    return fit_global(
        fghz, power_dbm, re_r, **LORENTZIAN, p0=p0, bounds=bounds,
        f_range=f_range, power_range=power_range, sigma=sigma,
        model_kwargs={"p_ref": p_ref, "p_norm": p_norm},
    )


def fit_fano(fghz, power_dbm, r, *, p_norm=None, xi=1.0, f_range=None,
             power_range=None, sigma=None, fixed=None, p0=None, bounds=None):
    """Global complex fit of eq. (G.1) [SC25] to r (Im r > 0 above f0).

    Free parameters: f0, Gamma1, Gamma_phi, A_sys (att_db), theta_F and a
    complex background; use ``fixed`` to hold any of them, e.g.
    ``fixed={"theta_f": 0}`` for the plain Lorentzian in the same model.
    """
    f_range = f_range or (fghz.min(), fghz.max())
    if p0 is None:
        p0 = [0.5 * (f_range[0] + f_range[1]), 20.0, 1.0, 100.0, 0.0, 0.0, 0.0]
    if bounds is None:
        bounds = ([f_range[0], 0.1, 0.0, 50.0, -np.pi / 2, -0.3, -0.3],
                  [f_range[1], 200.0, 100.0, 150.0, np.pi / 2, 0.3, 0.3])
    return fit_global(
        fghz, power_dbm, np.asarray(r, dtype=complex), **FANO, p0=p0, bounds=bounds,
        f_range=f_range, power_range=power_range, sigma=sigma, fixed=fixed,
        model_kwargs={"p_norm": p_norm, "xi": xi},
    )


# ---------------------------------------------------------------------------
# Power calibration
# ---------------------------------------------------------------------------

def dbm_to_watt(p_dbm):
    return 1e-3 * 10 ** (np.asarray(p_dbm) / 10)


def resonance_value(fghz, r, f0, window_mhz=0.0):
    """r (real or complex) at f0 for each power: interpolated, or averaged over ±window."""
    fghz = np.asarray(fghz)
    r = np.asarray(r)
    if window_mhz > 0:
        m = np.abs(fghz - f0) * 1000 <= window_mhz
        return r[:, m].mean(axis=1)
    if np.iscomplexobj(r):
        return np.array([np.interp(f0, fghz, row.real) + 1j * np.interp(f0, fghz, row.imag) for row in r])
    return np.array([np.interp(f0, fghz, row) for row in r])


def power_at_qubit(r_on, g1, g2, f0):
    """Drive power W0 [W] at the qubit from on-resonance Re r (eq. 4).

    g1, g2 in MHz (rate / 2 pi), f0 in GHz. ``r_on`` must already have the
    fitted background removed.
    """
    gamma1 = 2 * np.pi * g1 * 1e6
    gamma2 = 2 * np.pi * g2 * 1e6
    return (gamma1 / (4 * np.asarray(r_on)) - gamma2 / 2) * H_PLANCK * f0 * 1e9


def power_from_omega(omega, g1, f0):
    """W0 [W] from the Rabi frequency: W0 = h f0 Omega^2 / (2 Gamma1)."""
    return H_PLANCK * f0 * 1e9 * (2 * np.pi * omega * 1e6) ** 2 / (2 * 2 * np.pi * g1 * 1e6)


def qubit_params(fit):
    """Physical parameters of any supported fit as a dict.

    f0 [GHz], g1, g2 [MHz], complex ``prefactor`` multiplying the ideal r
    (xi e^{i theta_F}, or 1), ``background`` and the attenuation [dB]
    implied by the global fit.
    """
    names = fit.names
    g1 = fit["g1"]
    g2 = fit["g2"] if "g2" in names else g1 / 2 + fit["gphi"]
    if "theta_f" in names:
        prefactor = fit.model_kwargs.get("xi", 1.0) * np.exp(1j * fit["theta_f"])
        background = fit["bg_re"] + 1j * fit["bg_im"]
    else:
        prefactor, background = 1.0, fit["background"]
    if "att_db" in names:
        att_db = fit["att_db"]
    else:
        w0_ref = power_from_omega(fit["omega_ref"], g1, fit["f0"])
        att_db = 10 * np.log10(dbm_to_watt(fit.model_kwargs["p_ref"]) / w0_ref)
    return dict(f0=fit["f0"], g1=g1, g2=g2, prefactor=prefactor,
                background=background, att_db=float(att_db))


@dataclass
class AttenuationResult:
    power_dbm: np.ndarray     # all powers
    w_in: np.ndarray          # W at VNA output
    w0: np.ndarray            # W at qubit (eq. 4 of [HD20])
    mask: np.ndarray          # points used in the slope fit
    slope: float              # W0 / Win
    attenuation_db: float     # from slope fit (original notebook)
    attenuation_fit_db: float  # implied by the global fit (omega_ref or A_sys)

    @property
    def per_point_db(self):
        """10 log10(Win / W0) per power; NaN where W0 <= 0."""
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(self.w0 > 0, 10 * np.log10(self.w_in / self.w0), np.nan)

    def to_dict(self):
        return {
            "attenuation_db": float(self.attenuation_db),
            "attenuation_fit_db": float(self.attenuation_fit_db),
            "slope": float(self.slope),
            "fit_powers_dbm": self.power_dbm[self.mask].tolist(),
            "power_dbm": self.power_dbm.tolist(),
            "w_in_W": self.w_in.tolist(),
            "w0_W": self.w0.tolist(),
            "per_point_db": [None if np.isnan(v) else float(v) for v in self.per_point_db],
        }


def fit_attenuation(fit, fghz, power_dbm, r, power_range=None, window_mhz=0.0):
    """Attenuation VNA output -> qubit, per power from eq. 4 of [HD20].

    ``r`` is the data the fit was made to (Re r or complex r). The on-resonance
    value has the fitted background and prefactor removed before eq. 4. The
    slope of W0 vs Win through the origin over ``power_range`` (original
    notebook) is compared with the attenuation implied by the global fit.
    """
    power_dbm = np.asarray(power_dbm)
    q = qubit_params(fit)
    r_on = (resonance_value(fghz, r, q["f0"], window_mhz) - q["background"]) / q["prefactor"]
    w_in = dbm_to_watt(power_dbm)
    w0 = power_at_qubit(np.real(r_on), q["g1"], q["g2"], q["f0"])

    lo, hi = power_range or (power_dbm.min(), power_dbm.max())
    mask = (power_dbm >= lo) & (power_dbm <= hi)
    slope = np.dot(w_in[mask], w0[mask]) / np.dot(w_in[mask], w_in[mask])

    return AttenuationResult(
        power_dbm=power_dbm, w_in=w_in, w0=w0, mask=mask, slope=float(slope),
        attenuation_db=float(-10 * np.log10(slope)),
        attenuation_fit_db=q["att_db"],
    )


# ---------------------------------------------------------------------------
# Plotting (each returns the Figure so the notebook can save it)
# ---------------------------------------------------------------------------

def _power_colors(power_dbm, cmap="viridis"):
    p = np.asarray(power_dbm, dtype=float)
    span = np.ptp(p) or 1.0
    return plt.get_cmap(cmap)((p - p.min()) / span)


def _power_colorbar(fig, ax, power_dbm, cmap="viridis"):
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(np.min(power_dbm), np.max(power_dbm)))
    fig.colorbar(sm, ax=ax, label="VNA power [dBm]")


def plot_traces(fghz, power_dbm, y, every=1, ylabel="|r|", ax=None):
    """Overlay one trace per power, colored by power."""
    fig, ax = (ax.figure, ax) if ax else plt.subplots(figsize=(8, 5))
    colors = _power_colors(power_dbm)
    for i in range(0, len(power_dbm), every):
        ax.plot(fghz, y[i], color=colors[i], lw=1)
    _power_colorbar(fig, ax, power_dbm)
    ax.set_xlabel("Frequency [GHz]")
    ax.set_ylabel(ylabel)
    ax.grid(alpha=0.3)
    return fig


def plot_map(fghz, power_dbm, y, label="Re r", ax=None, **kw):
    """Color map of ``y`` versus frequency and power."""
    fig, ax = (ax.figure, ax) if ax else plt.subplots(figsize=(8, 5))
    m = ax.pcolormesh(fghz, power_dbm, y, shading="nearest", **kw)
    fig.colorbar(m, ax=ax, label=label)
    ax.set_xlabel("Frequency [GHz]")
    ax.set_ylabel("VNA power [dBm]")
    return fig


_PART = {"real": (np.real, r"$\mathrm{Re}\,r$"),
         "imag": (np.imag, r"$\mathrm{Im}\,r$")}


def plot_fit(fit, fghz, power_dbm, r, every=1, offset=0.0, part="real", ax=None):
    """Data (dots) and global fit (lines) for the fitted powers.

    ``r`` is the data the fit was made to; ``part`` selects Re or Im.
    ``offset`` shifts successive traces vertically for readability.
    """
    fig, ax = (ax.figure, ax) if ax else plt.subplots(figsize=(8, 6))
    take, label = _PART[part]
    f = np.asarray(fghz)[fit.fmask]
    idx = np.flatnonzero(fit.pmask)[::every]
    colors = _power_colors(np.asarray(power_dbm))
    for k, i in enumerate(idx):
        p = power_dbm[i]
        ax.plot(f, take(r[i, fit.fmask]) + k * offset, ".", ms=3, color=colors[i])
        ax.plot(f, take(fit.evaluate(f, p)) + k * offset, "-", lw=1.2, color=colors[i])
    _power_colorbar(fig, ax, power_dbm)
    ax.set_xlabel("Frequency [GHz]")
    ax.set_ylabel(label + (" (offset)" if offset else ""))
    ax.grid(alpha=0.3)
    return fig


def _model_grid(fit, fghz, power_dbm):
    f = np.asarray(fghz)[fit.fmask]
    p = np.asarray(power_dbm)[fit.pmask]
    F, P = np.meshgrid(f, p)
    return f, p, fit.evaluate(F.ravel(), P.ravel()).reshape(F.shape)


def plot_fit_residuals(fit, fghz, power_dbm, r, part="real", ax=None):
    """Map of data - model over the fitted region."""
    take, label = _PART[part]
    f, p, model = _model_grid(fit, fghz, power_dbm)
    resid = take(np.asarray(r)[np.ix_(fit.pmask, fit.fmask)] - model)
    lim = np.nanpercentile(np.abs(resid), 99)
    return plot_map(f, p, resid, label=label + r" data $-$ model", ax=ax,
                    cmap="RdBu_r", vmin=-lim, vmax=lim)


def plot_fit_maps(fit, fghz, power_dbm, r):
    """Data vs model maps of Re r and Im r (cf. Fig. 11(a) of [SC25])."""
    f, p, model = _model_grid(fit, fghz, power_dbm)
    data = np.asarray(r)[np.ix_(fit.pmask, fit.fmask)]
    fig, axes = plt.subplots(2, 2, figsize=(11, 8), sharex=True, sharey=True)
    for row, part in enumerate(("real", "imag")):
        take, label = _PART[part]
        lim = np.nanpercentile(np.abs(take(data)), 99.5)
        vmin = 0 if part == "real" else -lim
        for col, (z, title) in enumerate(((data, "Experiment"), (model, "Theory"))):
            ax = axes[row, col]
            m = ax.pcolormesh(f, p, take(z), shading="nearest", cmap="RdBu_r", vmin=vmin, vmax=lim)
            fig.colorbar(m, ax=ax, label=label)
            ax.set_title(title)
    for ax in axes[-1]:
        ax.set_xlabel("Frequency [GHz]")
    for ax in axes[:, 0]:
        ax.set_ylabel("VNA power [dBm]")
    fig.tight_layout()
    return fig


def plot_power_cut(fit, fghz, power_dbm, r, f_ghz=None, att_db=None):
    """|t|^2 = |1 - r|^2 vs power at one frequency (cf. Fig. 11(b) of [SC25]).

    Default frequency is the data point closest to the fitted f0. With
    ``att_db`` the x axis is the power at the qubit.
    """
    fghz = np.asarray(fghz)
    power_dbm = np.asarray(power_dbm)
    i = int(np.argmin(np.abs(fghz - (fit["f0"] if f_ghz is None else f_ghz))))
    shift = 0.0 if att_db is None else att_db
    p_fine = np.linspace(power_dbm.min(), power_dbm.max(), 300)
    model = fit.evaluate(np.full_like(p_fine, fghz[i]), p_fine)
    if not fit.is_complex:  # only Re r is modelled
        data, model, ylabel = np.real(r[:, i]), model, r"$\mathrm{Re}\,r$"
    else:
        data, model, ylabel = np.abs(1 - r[:, i]) ** 2, np.abs(1 - model) ** 2, r"$|t|^2$"
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.plot(power_dbm - shift, data, "o", label="Experiment")
    ax.plot(p_fine - shift, model, "-", label="Theory")
    ax.set_xlabel("Power at qubit [dBm]" if att_db is not None else "VNA power [dBm]")
    ax.set_ylabel(ylabel)
    ax.set_title(f"{fghz[i]:.5f} GHz")
    ax.legend()
    ax.grid(alpha=0.3)
    return fig


def plot_attenuation(att):
    """W0 vs Win (log-log) and per-point attenuation vs power."""
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4.5))
    ok = att.w0 > 0
    a1.loglog(att.w_in[ok & ~att.mask], att.w0[ok & ~att.mask], "o", mfc="none", color="gray", label="not fitted")
    a1.loglog(att.w_in[ok & att.mask], att.w0[ok & att.mask], "o", label="fitted")
    x = np.geomspace(att.w_in.min(), att.w_in.max(), 100)
    a1.loglog(x, att.slope * x, "-", label=f"slope fit: {att.attenuation_db:.2f} dB")
    a1.loglog(x, x * 10 ** (-att.attenuation_fit_db / 10), "--",
              label=f"global fit: {att.attenuation_fit_db:.2f} dB")
    a1.set_xlabel(r"$W_\mathrm{in}$ [W]")
    a1.set_ylabel(r"$W_0$ [W]")
    a1.legend()
    a1.grid(alpha=0.3, which="both")

    a2.plot(att.power_dbm, att.per_point_db, "o", mfc="none", color="gray")
    a2.plot(att.power_dbm[att.mask], att.per_point_db[att.mask], "o")
    a2.axhline(att.attenuation_db, color="C1", label="slope fit")
    a2.axhline(att.attenuation_fit_db, color="C2", ls="--", label="global fit")
    a2.set_xlabel("VNA power [dBm]")
    a2.set_ylabel(r"$10\log_{10}(W_\mathrm{in}/W_0)$ [dB]")
    a2.legend()
    a2.grid(alpha=0.3)
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.ndarray):
        return _jsonable(o.tolist())
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, Path):
        return str(o)
    return o


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(_jsonable(data), f, indent=4)
    return path
