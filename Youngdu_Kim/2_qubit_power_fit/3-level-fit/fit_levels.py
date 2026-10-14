"""Fit a qubit_power_sweep with an N-level transmon scattering model, normalized by a
no-qubit reference trace (qubit flux-detuned, so t_ref = 1).

Model per trace j (VNA convention):
    S_j(f) / S_ref(f) = A_j * [1 - K (1 - t(f; P_j))]
    t = conj(1 - i (Gamma_r / Omega) <b>_ss),  Gamma_r = Gamma_1 = 2 gamma_2 (no pure dephasing)
    H/hbar = sum_n [n D + (alpha/2) n(n-1)] |n><n| + (Omega/2)(b + b^dag),  D = w01 - w
    s = Omega^2 / (Gamma_1 gamma_2) = k * P_VNA[mW]
N_L = 2 is the usual two-level (resonance fluorescence) formula. A_j (complex, per trace) is
solved in closed form at every step (variable projection), so the nonlinear parameters are
only f01, alpha (N_L > 2), gamma_2, |K|, arg K, log10 k.

usage: python fit_levels.py sweep.json ref.json NL PMAX out_prefix [F_LO F_HI]   (MHz)
"""
import json, sys, time
import numpy as np
from scipy.optimize import least_squares
from scipy.constants import hbar

NAMES = ["f01 (MHz)", "alpha/2pi (MHz)", "gamma2/2pi (MHz)", "|K|", "arg K (rad)", "log10 k"]


def load(sweep_file, ref_file):
    d = json.load(open(sweep_file)); r = json.load(open(ref_file))
    f = np.array(d["frequency_hz"])/1e6                  # MHz; all rates are in MHz (= rate/2pi)
    S = np.array(d["s_real"]) + 1j*np.array(d["s_imag"])
    R = np.array(r["trace"]["y_real"]) + 1j*np.array(r["trace"]["y_imag"])
    assert np.allclose(np.array(r["trace"]["x"])/1e6, f)
    Sn = S/R
    sig = np.array([np.std(np.diff(x))/np.sqrt(2) for x in Sn])   # white noise per trace
    return f, np.array(d["power_dbm"]), Sn, sig


class Transmon:
    """Steady-state transmission of an NL-level transmon (Duffing, sqrt(n) elements)."""

    def __init__(self, NL):
        self.NL = NL
        n = np.arange(NL); I = np.eye(NL)
        b = np.diag(np.sqrt(n[1:]), 1).astype(complex)
        comm = lambda A: np.kron(I, A) - np.kron(A.T, I)
        CdC = b.conj().T @ b
        self.L_n, self.L_nn = comm(np.diag(n*1.0)), comm(np.diag(n*(n-1)/2.0))
        self.L_x = comm(b + b.conj().T)
        self.D_b = np.kron(b.conj(), b) - 0.5*np.kron(I, CdC) - 0.5*np.kron(CdC.T, I)
        self.trace_row = I.reshape(-1, order="F")
        self.b_vec = b.T.reshape(-1, order="F")          # Tr(b rho) = b_vec . vec(rho)

    def t(self, Fv, fq, alpha, g2, s):
        """Ideal transmission (r0 = 1) at drive frequencies Fv (MHz), VNA convention."""
        NL = self.NL; G1 = 2*g2; Om = max(np.sqrt(s*G1*g2), 1e-9)
        L = -1j*((fq - Fv)[:, None, None]*self.L_n + alpha*self.L_nn + Om/2*self.L_x) + G1*self.D_b
        L[:, 0, :] = self.trace_row
        rhs = np.zeros((len(Fv), NL*NL, 1), complex); rhs[:, 0] = 1
        rho = np.linalg.solve(L, rhs)[..., 0]
        return np.conj(1 - 1j*G1*(rho @ self.b_vec)/Om)


def unpack(p, NL):
    fq, alpha, g2, kabs, karg, lk = p if NL > 2 else (p[0], 0.0, *p[1:])
    return fq, alpha, g2, kabs*np.exp(1j*karg), 10**lk


def model_traces(p, tm, F, P):
    """1 - K (1 - t) for each power in P (before the per-trace scale A_j)."""
    fq, alpha, g2, K, k = unpack(p, tm.NL)
    return [1 - K*(1 - tm.t(F, fq, alpha, g2, k*10**(pw/10))) for pw in P]


def project(models, data):
    """Best complex scale per trace (closed form) and the scaled models."""
    A = np.array([np.vdot(m, y)/np.vdot(m, m) for m, y in zip(models, data)])
    return A, [a*m for a, m in zip(A, models)]


def fit(sweep_file, ref_file, NL, pmax, f_lo=5400.0, f_hi=5800.0):
    f, P, Sn, sig = load(sweep_file, ref_file)
    win = (f >= f_lo) & (f <= f_hi); F = f[win]
    idx = np.where(P <= pmax)[0]
    data = [Sn[i][win] for i in idx]; tm = Transmon(NL)

    def resid(p):
        _, scaled = project(model_traces(p, tm, F, P[idx]), data)
        r = np.concatenate([(y - m)/sig[i] for y, m, i in zip(data, scaled, idx)])
        return np.concatenate([r.real, r.imag])

    x0 = [5577.6, -220.0, 14.2, 1.04, 0.33, 2.53]
    if NL == 2: x0 = [x0[0]] + x0[2:]
    t0 = time.time()
    res = least_squares(resid, np.array(x0), x_scale="jac", diff_step=1e-6)
    r = resid(res.x); nw = win.sum()
    dof = len(r) - (len(res.x) + 2*len(idx)); chi2 = float(np.sum(r**2))
    err = np.sqrt(np.abs(np.diag(np.linalg.pinv(res.jac.T @ res.jac)*chi2/dof)))
    e = dict(zip(NAMES if NL > 2 else [NAMES[0]] + NAMES[2:], err))
    fq, alpha, g2, K, k = unpack(res.x, NL)
    psat = -10*np.log10(k)
    pin = 10*np.log10(hbar*(2*np.pi*fq*1e6)*(2*np.pi*g2*1e6)/2/1e-3)
    rr = r.reshape(2, -1)
    return {
        "NL": NL, "pmax_dbm": pmax, "window_mhz": [f_lo, f_hi], "n_traces": int(len(idx)),
        "chi2": chi2, "dof": int(dof), "red_chi2": chi2/dof, "runtime_s": round(time.time() - t0, 1),
        "status": int(res.status), "x": [float(v) for v in res.x],
        "f01_mhz": float(fq), "f01_err": float(e["f01 (MHz)"]),
        "alpha_mhz": float(alpha) if NL > 2 else None,
        "alpha_err": float(e["alpha/2pi (MHz)"]) if NL > 2 else None,
        "gamma2_mhz": float(g2), "gamma2_err": float(e["gamma2/2pi (MHz)"]),
        "K_abs": float(abs(K)), "K_arg": float(np.angle(K)),
        "psat_vna_dbm": float(psat), "psat_vna_err": float(10*e["log10 k"]),
        "pin_sat_dbm": float(pin), "attenuation_db": float(psat - pin),
        "red_chi2_per_trace": {f"{P[i]:g}": float(np.sum(rr[:, j*nw:(j+1)*nw]**2)/(2*nw))
                               for j, i in enumerate(idx)},
    }


if __name__ == "__main__":
    sweep_file, ref_file, NL, pmax, out = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
    window = (float(sys.argv[6]), float(sys.argv[7])) if len(sys.argv) > 7 else (5400.0, 5800.0)
    summary = fit(sweep_file, ref_file, NL, pmax, *window)
    json.dump(summary, open(out + ".json", "w"), indent=1)
    print(json.dumps({k: v for k, v in summary.items() if k not in ("red_chi2_per_trace", "x")}))
