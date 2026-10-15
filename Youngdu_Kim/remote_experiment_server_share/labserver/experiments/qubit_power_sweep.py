"""Qubit power sweep: where does the qubit power-calibration sensor saturate?

Steps the drive power and records S21 around the qubit at every step, in one of two ways:
  * "vna": steps the VNA output power (like vna-power-sweep.ipynb, which swept
    SOURce1:POWer from -30 to -10 dBm with 100 traces per point). The attenuator is
    not touched; its value is in the saved snapshot.
  * "attenuator": keeps the VNA power fixed and steps the warm programmable attenuator
    (RCDAT) instead, so the VNA's own AUTO step attenuator never switches during the
    sweep. The attenuator is set to the first point before the RF is turned on, and
    restored after the VNA.

The range is given as segments (from, to, step, traces per point), so low powers, where
the noise is worse, can get more averaging than high ones. Averaging is the mean of
single-sweep complex traces, as in the notebook.

Result: s_real/s_imag[point][frequency] (the mean at each point), a 2D map in the UI,
with y = power_dbm (VNA power) or attenuation_db. In attenuator mode the record also
has drive_dbm = VNA power - attenuation, the power after the attenuator.
"""
from __future__ import annotations

from ..drivers.rcdat import STEP_DB as ATT_STEP_DB
from .base import Param
from .vna_sweep import VnaMapSweep, sweep_values

ATT = "attenuator"

SWEEP = "Power sweep"
VNA = "VNA trace at each point"
END = "Before and after"
SAVE = "Save"


def segment_points(segments: list[dict], start: str = "start_dbm", stop: str = "stop_dbm", max_points: int = 2000
                   ) -> tuple[list[tuple[float, int]], list[str], list[str]]:
    """[(value, averages)] in sweep order, plus (errors, warnings). ``start``/``stop``
    name the columns (power or attenuation segments). A point shared by the end of one
    segment and the start of the next is measured once (with the first segment's
    averages)."""
    points, errors, warnings = [], [], []
    if not segments:
        errors.append("add at least one segment")
    for i, seg in enumerate(segments, 1):
        if seg["step_db"] <= 0:
            errors.append(f"segment {i}: step must be > 0")
            continue
        if seg["averages"] < 1:
            errors.append(f"segment {i}: traces per point must be >= 1")
            continue
        if abs(seg[stop] - seg[start]) / seg["step_db"] + 1 > max_points:
            errors.append(f"segment {i}: more than {max_points} points; use a larger step")
            continue
        values, warn = sweep_values(seg[start], seg[stop], seg["step_db"])
        if warn:
            warnings.append(f"segment {i}: {warn}")
        if points and values and abs(points[-1][0] - values[0]) < 1e-9:
            values = values[1:]
        points += [(v, seg["averages"]) for v in values]
    seen, dup = set(), set()
    for v, _ in points:
        (dup if v in seen else seen).add(v)
    if dup:
        errors.append("segments overlap; each point once please: "
                      + ", ".join(f"{v:g}" for v in sorted(dup)[:5]) + (" ..." if len(dup) > 5 else ""))
    return points, errors, warnings


class QubitPowerSweep(VnaMapSweep):
    id = "qubit_power_sweep"
    title = "Qubit power sweep"
    summary = ("Step the drive power (VNA output power, or the warm step attenuator at a fixed VNA "
               "power) and record S21 around the qubit at each step (averaged), to find where the "
               "power-calibration sensor saturates. Result: 2D map, frequency x power (or attenuation).")
    instruments = ["vna", "warm_switches", ATT]     # the attenuator is reserved in both modes
    turns_on = "the VNA RF output"
    y_key, y_label, y_unit = "power_dbm", "VNA power", "dBm"
    params = [
        Param("power_control", "step the power with", kind="select", default="vna", group=SWEEP,
              options=[["vna", "VNA output power"],
                       [ATT, "warm step attenuator (VNA power fixed)"]],
              help="Step attenuator: the VNA power stays fixed, so the VNA's own AUTO step attenuator "
                   "never switches during the sweep; the warm RCDAT (0-63 dB in 0.25 dB steps) is "
                   "stepped instead."),
        Param("segments", "power segments", kind="table", group=SWEEP, show_if={"power_control": ["vna"]},
              default=[{"start_dbm": -80.0, "stop_dbm": -60.0, "step_db": 2.0, "averages": 100},
                       {"start_dbm": -60.0, "stop_dbm": -30.0, "step_db": 1.0, "averages": 30},
                       {"start_dbm": -30.0, "stop_dbm": 0.0, "step_db": 1.0, "averages": 10}],
              columns=[Param("start_dbm", "from", unit="dBm"), Param("stop_dbm", "to", unit="dBm"),
                       Param("step_db", "step", unit="dB"),
                       Param("averages", "traces per point", kind="int")],
              help="Swept top to bottom. Lower power is noisier, so give it more traces per point. "
                   "Limit: vna power in the settings tab. Our ZVA24 (port 1 step attenuator B21, "
                   "AUTO) covers about -100..+11 dBm at 5-6 GHz; its 10 dB attenuator steps switch "
                   "as the power crosses -30, -40, ... dBm."),
        Param("vna_power_dbm", "VNA power (fixed)", unit="dBm", default=-10.0, group=SWEEP,
              show_if={"power_control": [ATT]},
              help="Kept for the whole sweep. Drive after the attenuator = this - attenuation."),
        Param("att_segments", "attenuation segments", kind="table", group=SWEEP,
              show_if={"power_control": [ATT]},
              default=[{"start_db": 63.0, "stop_db": 55.0, "step_db": 2.0, "averages": 30},
                       {"start_db": 55.0, "stop_db": 45.0, "step_db": 2.0, "averages": 10},
                       {"start_db": 45.0, "stop_db": 35.0, "step_db": 2.0, "averages": 3},
                       {"start_db": 35.0, "stop_db": 15.0, "step_db": 2.0, "averages": 1}],
              columns=[Param("start_db", "from", unit="dB"), Param("stop_db", "to", unit="dB"),
                       Param("step_db", "step", unit="dB"),
                       Param("averages", "traces per point", kind="int")],
              help="Swept top to bottom: start at high attenuation (low power). Multiples of 0.25 dB. "
                   "Limit: attenuator attenuation in the settings tab. The default with the VNA at "
                   "-10 dBm gives the same drive as VNA powers -48..0 dBm at 25 dB."),
        # trace at each point
        Param("start_hz", "start", unit="GHz", scale=1e9, default=5.525e9, group=VNA, from_state=["vna", "start_hz"]),
        Param("stop_hz", "stop", unit="GHz", scale=1e9, default=5.625e9, group=VNA, from_state=["vna", "stop_hz"]),
        Param("points", "points", kind="int", default=401, group=VNA, from_state=["vna", "points"]),
        Param("ifbw_hz", "IFBW", unit="Hz", default=1e3, group=VNA, from_state=["vna", "ifbw_hz"]),
        Param("save_all_traces", "save every trace (not only the mean)", kind="bool", default=False, group=VNA),
        Param("settle_s", "wait after each power step", unit="s", default=0.2, group=VNA),
        # before / after
        Param("set_switches", "set the warm switches to VNA -> fridge -> VNA first", kind="bool",
              default=True, group=END),
        Param("finish", "when done", kind="select", default="restore", group=END,
              options=[["restore", "restore the VNA, switches and attenuator as they were"],
                       ["last", "leave everything at the last point"],
                       ["rf_off", "turn the VNA RF output off"]]),
        # save
        Param("label", "label", kind="text", default="qubit-power-sweep", group=SAVE),
        Param("note", "note", kind="text", default="", group=SAVE),
    ]

    def y_axis(self, p):
        if p["power_control"] == ATT:
            return "attenuation_db", "attenuator", "dB"
        return self.y_key, self.y_label, self.y_unit

    def plan(self, p: dict, mgr=None) -> dict:
        notes = []
        max_points = self.cap("max_points")
        if p["power_control"] == ATT:
            points, errors, warnings = segment_points(p["att_segments"], "start_db", "stop_db", max_points)
            self.check_limit(errors, "vna", "power_dbm", p["vna_power_dbm"], "VNA power", "dBm")
            self.check_limit(errors, ATT, "attenuation_db", [v for v, _ in points], "attenuation", "dB")
            off_grid = sorted({v for v, _ in points if abs(v / ATT_STEP_DB - round(v / ATT_STEP_DB)) > 1e-9})
            if off_grid:
                errors.append(f"attenuation not a multiple of {ATT_STEP_DB:g} dB: "
                              + ", ".join(f"{v:g}" for v in off_grid[:5]))
            if points:
                pv, a0, a1 = p["vna_power_dbm"], points[0][0], points[-1][0]
                notes.append(f"{sum(n for _, n in points)} traces in all; attenuator {a0:g} -> {a1:g} dB "
                             f"at a fixed VNA power of {pv:g} dBm (drive after the attenuator "
                             f"{pv - a0:g} -> {pv - a1:g} dBm)")
        else:
            points, errors, warnings = segment_points(p["segments"], max_points=max_points)
            self.check_limit(errors, "vna", "power_dbm", [v for v, _ in points], "VNA power", "dBm")
            if points:
                notes.append(f"{sum(n for _, n in points)} traces in all; VNA power {points[0][0]:g} -> "
                             f"{points[-1][0]:g} dBm; the attenuator is not changed")
        self.check_vna(p, errors)
        return self.make_plan(p, points, errors, warnings, notes, mgr)

    def check_instruments(self, mgr, p, points, errors, warnings, notes):
        super().check_instruments(mgr, p, points, errors, warnings, notes)
        if p["power_control"] != ATT or not points:
            return
        try:
            att = self.read_state(mgr, ATT)["attenuation_db"]
        except Exception as e:
            warnings.append(f"could not read the attenuator: {e}")
            return
        then = "restored after the sweep" if p["finish"] == "restore" else "not restored after the sweep"
        notes.append(f"attenuator is at {att:g} dB now; set to {points[0][0]:g} dB before the RF is "
                     f"turned on, {then}")

    # ---- run hooks ----
    def vna_power_dbm(self, p, points):
        return p["vna_power_dbm"] if p["power_control"] == ATT else points[0][0]

    def prepare(self, p, job, points, snapshot):
        if p["power_control"] != ATT:
            return
        st = snapshot.get(ATT, {})
        if "error" in st:
            raise RuntimeError(f"cannot read the attenuator: {st['error']}")
        job.progress(f"setting the attenuator to {points[0][0]:g} dB")
        job.mgr.apply(ATT, job.owner, attenuation_db=points[0][0])

    def step(self, p, job, value):
        if p["power_control"] == ATT:
            job.mgr.apply(ATT, job.owner, attenuation_db=value)
        else:
            job.mgr.apply("vna", job.owner, power_dbm=value)

    def record_point(self, p, rec, value):
        if p["power_control"] == ATT:
            rec.setdefault("drive_dbm", []).append(round(p["vna_power_dbm"] - value, 6))

    def _restore_att(self, p, snapshot) -> float | None:
        st0 = snapshot.get(ATT, {})
        if p["power_control"] == ATT and p["finish"] == "restore" and "error" not in st0:
            return st0["attenuation_db"]
        return None

    def finish_more(self, p, job, snapshot, done):
        """Before the VNA is restored: raise the attenuation to the old value if that is
        higher, so the drive never exceeds both the sweep's and the old one."""
        old = self._restore_att(p, snapshot)
        if old is not None and old > job.mgr.get_state(ATT, job.owner)["attenuation_db"]:
            job.mgr.apply(ATT, job.owner, attenuation_db=old)

    def finish_last(self, p, job, snapshot, done):
        """After the VNA is restored: the old attenuation (lower, if it was not set above)."""
        old = self._restore_att(p, snapshot)
        if old is not None:
            job.mgr.apply(ATT, job.owner, attenuation_db=old)
            done.append("attenuator restored")
