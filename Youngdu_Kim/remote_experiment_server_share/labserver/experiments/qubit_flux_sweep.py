"""Qubit flux sweep: qubit frequency vs flux bias, the first thing to measure.

Steps the GS210 flux bias voltage and records S21 around the qubit at every step, with
the VNA band, IFBW, power and traces per point fixed (like qubit-flux-sweep.ipynb,
which swept -0.28..-0.24 V in 201 points with one trace each). The dip follows the
qubit frequency, so the map shows f_q(V); pick the bias for the power sweep from it.

Flux bias: if its output is off it is turned on at 0 V and ramped to the first point
(the driver ramps any change larger than ramp_step). The range is set only when it is
too small for the sweep and the output is off; with the output on that is an error.

Result: s_real/s_imag[flux][frequency] (the mean at each bias), a 2D map in the UI.
"""
from __future__ import annotations

from ..drivers.gs210 import VOLT_RANGES
from ..drivers.base import within
from .base import Param
from .vna_sweep import VnaMapSweep, sweep_values

FLUX = "qubit_flux_bias"

SWEEP = "Flux bias sweep"
VNA = "VNA trace at each point"
END = "Before and after"
SAVE = "Save"


def flux_setup(state: dict, values: list[float]) -> tuple[float | None, str | None, str]:
    """(range to set or None, error or None, note) for sweeping the source over values."""
    func = str(state.get("function", "")).upper()
    if not func.startswith("VOLT"):
        return None, f"the flux bias source is in {func or '?'} mode; set it to VOLT", ""
    need = max(abs(v) for v in values)
    rng = None
    if state["range"] < need * (1 - 1e-9):
        if state["output"]:
            return None, (f"the flux bias range is {state['range']:g} V, too small for {need:g} V; "
                          f"turn its output off (the sweep then sets the range) or change the range"), ""
        rng = next(r for r in VOLT_RANGES if r >= need * (1 - 1e-9))
    if state["output"]:
        note = f"flux bias output is on at {state['level']:g} V; ramps to {values[0]:g} V"
    else:
        note = f"flux bias output is off: turned on at 0 V, then ramped to {values[0]:g} V"
    if rng:
        note += f"; range {state['range']:g} -> {rng:g} V first"
    return rng, None, note


def flux_start(job, st: dict, values: list[float], what: str = "the flux bias"):
    """Turn the flux bias on (at 0 V, if it is off) and ramp it to values[0]. st is its
    state before; raises if flux_setup() finds a problem."""
    mgr, owner = job.mgr, job.owner
    if "error" in st:
        raise RuntimeError(f"cannot read the flux bias: {st['error']}")
    rng, err, _ = flux_setup(st, values)
    if err:
        raise RuntimeError(err)
    if not st["output"]:
        job.progress(f"turning {what} on at 0 V")
        mgr.apply(FLUX, owner, **({"range": rng} if rng else {}), level=0.0)
        mgr.apply(FLUX, owner, output=True)
    job.progress(f"ramping {what} to {values[0]:g} V")
    mgr.apply(FLUX, owner, level=values[0])


def flux_finish(job, st0: dict, how: str, limits: dict, done: list[str]):
    """how = "restore": back to st0 (ramped; if it was off: ramped to 0 V, turned off,
    then its old range and level set with the output off), "off": ramped to 0 V and off."""
    mgr, owner = job.mgr, job.owner
    if how == "restore" and "error" not in st0:
        if st0["output"]:
            mgr.apply(FLUX, owner, level=st0["level"])        # ramped
        else:
            mgr.apply(FLUX, owner, level=0.0)
            mgr.apply(FLUX, owner, output=False)
            if within(limits.get("level"), st0["level"]):
                mgr.apply(FLUX, owner, range=st0["range"], level=st0["level"])     # output off
            else:
                job.notes.append(f"flux bias left at 0 V: the old level {st0['level']:g} V is outside the limits")
        done.append("flux bias restored")
    elif how == "off":
        mgr.apply(FLUX, owner, level=0.0)
        mgr.apply(FLUX, owner, output=False)
        done.append("flux bias at 0 V, off")


class QubitFluxSweep(VnaMapSweep):
    id = "qubit_flux_sweep"
    title = "Qubit flux sweep"
    summary = ("Step the qubit flux bias (GS210 voltage) and record S21 around the qubit at each step "
               "(averaged), to find the qubit frequency vs flux. Result: 2D map, frequency x flux bias.")
    instruments = ["vna", "warm_switches", FLUX]
    turns_on = "the VNA RF output and the flux bias"
    y_key, y_label, y_unit = "flux_bias", "flux bias", "V"
    params = [
        Param("flux_start_v", "from", unit="V", default=-0.28, group=SWEEP),
        Param("flux_stop_v", "to", unit="V", default=-0.24, group=SWEEP),
        Param("flux_step_v", "step", unit="V", default=0.0002, group=SWEEP,
              help="Limit: qubit_flux_bias level (VOLT mode) in the settings tab. Changes larger than its "
                   "ramp_step are ramped. If the output is off it is turned on at 0 V and ramped "
                   "to the first point."),
        Param("averages", "traces per point", kind="int", default=1, group=SWEEP),
        Param("settle_s", "wait after each flux step", unit="s", default=0.05, group=SWEEP),
        # trace at each point
        Param("start_hz", "start", unit="GHz", scale=1e9, default=5.525e9, group=VNA, from_state=["vna", "start_hz"]),
        Param("stop_hz", "stop", unit="GHz", scale=1e9, default=5.625e9, group=VNA, from_state=["vna", "stop_hz"]),
        Param("points", "points", kind="int", default=401, group=VNA, from_state=["vna", "points"]),
        Param("ifbw_hz", "IFBW", unit="Hz", default=1e3, group=VNA, from_state=["vna", "ifbw_hz"]),
        Param("power_dbm", "power", unit="dBm", default=-50.0, group=VNA, from_state=["vna", "power_dbm"],
              help="Keep it below where the qubit saturates (see the power sweep)."),
        Param("save_all_traces", "save every trace (not only the mean)", kind="bool", default=False, group=VNA),
        # before / after
        Param("set_switches", "set the warm switches to VNA -> fridge -> VNA first", kind="bool",
              default=True, group=END),
        Param("finish", "when done", kind="select", default="restore", group=END,
              options=[["restore", "restore the VNA, switches and flux bias as they were"],
                       ["last", "leave everything at the last point"],
                       ["off", "turn the VNA RF off, ramp the flux bias to 0 V and turn it off"]]),
        # save
        Param("label", "label", kind="text", default="qubit-flux-sweep", group=SAVE),
        Param("note", "note", kind="text", default="", group=SAVE),
    ]

    def plan(self, p: dict, mgr=None) -> dict:
        errors, warnings, notes, points = [], [], [], []
        start, stop, step = p["flux_start_v"], p["flux_stop_v"], p["flux_step_v"]
        if step <= 0:
            errors.append("flux step must be > 0")
        elif abs(stop - start) / step + 1 > self.cap("max_points"):
            errors.append(f"more than {self.cap('max_points')} flux points; use a larger step")
        elif p["averages"] < 1:
            errors.append("traces per point must be >= 1")
        else:
            values, warn = sweep_values(start, stop, step, "V")
            if warn:
                warnings.append(warn)
            points = [(v, p["averages"]) for v in values]
        self.check_limit(errors, FLUX, "level", [v for v, _ in points], "flux bias", "V")
        self.check_limit(errors, "vna", "power_dbm", p["power_dbm"], "VNA power", "dBm")
        self.check_vna(p, errors)
        if points:
            notes.append(f"{len(points) * p['averages']} traces in all; flux bias {points[0][0]:g} -> "
                         f"{points[-1][0]:g} V at VNA power {p['power_dbm']:g} dBm; the attenuator is not changed")
        return self.make_plan(p, points, errors, warnings, notes, mgr)

    def check_instruments(self, mgr, p, points, errors, warnings, notes):
        super().check_instruments(mgr, p, points, errors, warnings, notes)
        if not points:
            return
        try:
            st = self.read_state(mgr, FLUX)
        except Exception as e:
            warnings.append(f"could not read the flux bias: {e}")
            return
        _, err, note = flux_setup(st, [v for v, _ in points])
        if err:
            errors.append(err)
        else:
            notes.append(note)

    # ---- run hooks ----
    def prepare(self, p, job, points, snapshot):
        flux_start(job, snapshot.get(FLUX, {}), [v for v, _ in points])

    def step(self, p, job, value):
        job.mgr.apply(FLUX, job.owner, level=value)

    def finish_more(self, p, job, snapshot, done):
        flux_finish(job, snapshot.get(FLUX, {}), p["finish"], self.limits(FLUX), done)
