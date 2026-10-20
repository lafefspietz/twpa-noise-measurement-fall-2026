"""Shared part of sweeps that record averaged VNA S21 around the qubit at each point of
one swept setting (VNA power, flux bias, ...). The result is a 2D map, frequency x the
swept setting.

A subclass names the swept setting (y_key, y_label, y_unit, or y_axis() when it depends
on the settings), lists its points in plan() as {"i", y_key, "averages"}, and sets it
with step(). Everything else is here: VNA
checks, warm switches, the averaged trace reads, the saved record and the finish step.
Averaging is the mean of single-sweep complex traces, as in the notebooks. Each point
records when its traces started and ended (point_start_time/point_end_time, epoch s),
so drifts in time can be told apart from the swept setting.
"""
from __future__ import annotations

import logging
import time

import numpy as np

from .base import Experiment

log = logging.getLogger("labserver.experiments")

SWITCH_PATH = {"fridge_in": "vna", "rx_source": "fridge", "receiver": "vna"}
TRACE_OVERHEAD_S = 0.05     # transfer + *OPC? per single sweep (rough)


def sweep_values(start: float, stop: float, step: float, unit: str = "dB") -> tuple[list[float], str | None]:
    """start..stop in steps of |step|, both ends included when stop is on the grid.
    Returns (values, warning)."""
    n = int(abs(stop - start) / step + 1e-9)
    sign = 1.0 if stop >= start else -1.0
    values = [round(start + sign * i * step, 6) for i in range(n + 1)]
    warning = None
    if abs(values[-1] - stop) > 1e-6:
        warning = f"stop {stop:g} is not on the {step:g} {unit} grid from {start:g}; the last point is {values[-1]:g}"
    return values, warning


def map_row(key: str, value: float, averages: int, mean) -> dict:
    """Result row of one point: |S21| dB and phase of the mean trace."""
    mean = np.asarray(mean, dtype=complex)
    return {key: value, "averages": averages,
            "s_db": np.round(20 * np.log10(np.maximum(np.abs(mean), 1e-30)), 4).tolist(),
            "deg": np.round(np.degrees(np.angle(mean)), 3).tolist()}


class VnaMapSweep(Experiment):
    y_key = ""          # key of the swept setting in plan points, result rows and the record
    y_label = ""
    y_unit = ""

    def y_axis(self, p: dict) -> tuple[str, str, str]:
        """(key, label, unit) of the swept setting for these settings."""
        return self.y_key, self.y_label, self.y_unit

    # ---- plan helpers ----
    def trace_time_s(self, p: dict) -> float:
        return p["points"] / p["ifbw_hz"] * 1.2 + TRACE_OVERHEAD_S if p["ifbw_hz"] > 0 else 0.0

    def check_vna(self, p: dict, errors: list[str]):
        if p["points"] < 2:
            errors.append("VNA points must be >= 2")
        if p["ifbw_hz"] <= 0:
            errors.append("IFBW must be > 0")
        if p["stop_hz"] <= p["start_hz"]:
            errors.append("VNA stop frequency must be above start")
        if p["settle_s"] < 0:
            errors.append("wait after each step must be >= 0")
        if p["points"] > self.cap("max_points"):
            errors.append(f"more than {self.cap('max_points')} VNA points")
        self.check_limit(errors, "vna", "start_hz", p["start_hz"], "VNA start", "Hz")
        self.check_limit(errors, "vna", "stop_hz", p["stop_hz"], "VNA stop", "Hz")
        self.check_limit(errors, "vna", "points", p["points"], "VNA points")
        self.check_limit(errors, "vna", "ifbw_hz", p["ifbw_hz"], "IFBW", "Hz")

    def make_plan(self, p: dict, points: list[tuple[float, int]], errors, warnings, notes, mgr=None) -> dict:
        """The plan dict from [(value, averages)], with time and size estimates."""
        traces = sum(n for _, n in points)
        estimate_s = len(points) * p["settle_s"] + traces * self.trace_time_s(p)
        saved = len(points) + (traces if p["save_all_traces"] else 0)
        size_mb = saved * 2 * p["points"] * 26 / 1e6        # indent=4 JSON, ~26 bytes per number
        if size_mb > self.cap("big_file_mb"):
            warnings.append(f"the data file would be about {size_mb:.0f} MB; turn off 'save every trace' "
                            f"or use fewer traces/points")
        if mgr is not None:
            self.check_instruments(mgr, p, points, errors, warnings, notes)
        key, label, unit = self.y_axis(p)
        return {"points": [{"i": i, key: v, "averages": n} for i, (v, n) in enumerate(points)],
                "estimate_s": round(estimate_s, 1), "size_mb": round(size_mb, 2),
                "columns": [["i", "#", ""], [key, label, unit], ["averages", "traces", ""]],
                "errors": errors, "warnings": warnings, "notes": notes}

    def check_instruments(self, mgr, p, points, errors, warnings, notes):
        """Reads only. Subclasses add their own instruments."""
        status = mgr.status()
        down = [n for n in self.instruments if n in status and not status[n]["connected"]]
        if down:
            warnings.append("not connected: " + ", ".join(down))
        try:
            sw = self.read_state(mgr, "warm_switches")
        except Exception as e:
            warnings.append(f"could not read the warm switches: {e}")
            return
        wrong = {k: sw.get(k) for k, v in SWITCH_PATH.items() if sw.get(k) != v}
        if not wrong:
            notes.append("warm switches are on the VNA -> fridge -> VNA path")
        elif p["set_switches"]:
            notes.append("will set warm switches: " + ", ".join(f"{k} {wrong[k]} -> {SWITCH_PATH[k]}" for k in wrong))
        else:
            warnings.append("warm switches are not on the VNA -> fridge -> VNA path: "
                            + ", ".join(f"{k}={v}" for k, v in wrong.items()))

    @staticmethod
    def read_state(mgr, name: str) -> dict:
        owner = mgr.slots[name].claimed_by or "plan"     # a running sweep holds it
        with mgr.use(name, owner=owner, timeout=2.0) as inst:
            return inst.get_state()

    # ---- result ----
    def map_axis(self, p: dict, freqs) -> dict:
        key, label, unit = self.y_axis(p)
        return {"x": np.asarray(freqs, dtype=float).tolist(), "x_unit": "Hz",
                "y_key": key, "y_label": label, "y_unit": unit}

    def result_view(self, rec: dict) -> dict:
        p = self.record_settings(rec)
        key = self.y_axis(p)[0]
        means = np.asarray(rec["s_real"], dtype=float) + 1j * np.asarray(rec["s_imag"], dtype=float)
        return {"axis": self.map_axis(p, rec["frequency_hz"]),
                "rows": [map_row(key, v, n, m) for v, n, m in zip(rec[key], rec["averages"], means)]}

    # ---- run hooks ----
    def vna_power_dbm(self, p: dict, points) -> float:
        """VNA power set before the first point."""
        return p["power_dbm"]

    def prepare(self, p: dict, job, points, snapshot: dict):
        """Before the VNA is set up (e.g. turn a bias on)."""

    def step(self, p: dict, job, value: float):
        """Set the swept setting to value."""
        raise NotImplementedError

    def record_point(self, p: dict, rec: dict, value: float):
        """Add per-point values to the record next to the swept setting (e.g. derived ones)."""

    def finish_more(self, p: dict, job, snapshot: dict, done: list[str]):
        """Subclass part of the finish step, before the VNA is restored."""

    def finish_last(self, p: dict, job, snapshot: dict, done: list[str]):
        """Subclass part of the finish step, after the VNA is restored or turned off."""

    # ---- run ----
    def run(self, p: dict, job) -> None:
        plan = self.plan(p)
        if plan["errors"]:
            raise ValueError("; ".join(plan["errors"]))
        key = self.y_axis(p)[0]
        points = [(pt[key], pt["averages"]) for pt in plan["points"]]
        mgr, owner = job.mgr, job.owner
        job.progress("reserving instruments", points_total=len(points), traces_total=sum(n for _, n in points))

        with mgr.claim(self.instruments, owner):
            snapshot = mgr.snapshot(owner=owner)
            vna0 = snapshot.get("vna", {})
            if "error" in vna0:
                raise RuntimeError(f"cannot read the VNA: {vna0['error']}")
            freqs = np.linspace(p["start_hz"], p["stop_hz"], p["points"])
            rec = job.record
            rec.update(kind="experiment", experiment=self.id, params=self.recorded_params(p), note=p["note"], sim=mgr.sim,
                       frequency_hz=freqs, fghz=freqs / 1e9, **{key: []}, averages=[], point_start_time=[], point_end_time=[],
                       s_real=[], s_imag=[], instruments=snapshot)
            if p["save_all_traces"]:
                rec.update(traces_real=[], traces_imag=[])
            job.axis = self.map_axis(p, freqs)
            try:
                self._measure(p, job, points, snapshot)
            finally:
                self._finish(p, job, snapshot)

    def _measure(self, p, job, points, snapshot):
        mgr, owner, rec = job.mgr, job.owner, job.record
        key, _, unit = self.y_axis(p)
        vna0 = snapshot["vna"]
        if p["set_switches"]:
            job.progress("setting the warm switches")
            mgr.apply("warm_switches", owner, **SWITCH_PATH)
        self.prepare(p, job, points, snapshot)
        setup = {"start_hz": p["start_hz"], "stop_hz": p["stop_hz"], "points": p["points"],
                 "ifbw_hz": p["ifbw_hz"], "power_dbm": self.vna_power_dbm(p, points)}
        if not str(vna0.get("sweep_type", "LIN")).upper().startswith("LIN"):
            setup["sweep_type"] = "LIN"
        job.progress("setting up the VNA")
        state = mgr.apply("vna", owner, **setup)
        if state["points"] != p["points"]:
            raise RuntimeError(f"VNA took {state['points']} points, asked for {p['points']}")
        mgr.apply("vna", owner, output=True)

        traces_done = 0
        for i, (value, n) in enumerate(points):
            job.check()
            job.progress(f"{value:g} {unit} ({i + 1}/{len(points)}), {n} traces", points_done=i)
            self.step(p, job, value)
            job.sleep(p["settle_s"])
            t0 = time.time()
            total = np.zeros(p["points"], dtype=complex)
            singles = []
            for _ in range(n):
                job.check()
                with mgr.use("vna", owner) as vna:
                    tr = vna.read_trace()
                total += tr.y
                if p["save_all_traces"]:
                    singles.append(tr.y)
                traces_done += 1
                job.progress(traces_done=traces_done)
            mean = total / n
            t1 = time.time()
            rec[key].append(value)
            self.record_point(p, rec, value)
            rec["averages"].append(n)
            rec["point_start_time"].append(round(t0, 3))
            rec["point_end_time"].append(round(t1, 3))
            rec["s_real"].append(mean.real)
            rec["s_imag"].append(mean.imag)
            if singles:
                rec["traces_real"].append([s.real for s in singles])
                rec["traces_imag"].append([s.imag for s in singles])
            job.add_row(map_row(key, value, n, mean))
        job.progress(f"{len(points)} points measured", points_done=len(points))

    def _finish(self, p, job, snapshot):
        """Leave the instruments as asked. After "outputs off" nothing is sent: the
        outputs are already off and every command would be refused."""
        mgr, owner = job.mgr, job.owner
        if mgr.halt.is_set():
            return
        vna0, sw0 = snapshot.get("vna", {}), snapshot.get("warm_switches", {})
        done = []
        try:
            self.finish_more(p, job, snapshot, done)
            if p["finish"] == "restore":
                restore = {k: vna0[k] for k in ("start_hz", "stop_hz", "points", "ifbw_hz", "power_dbm")
                           if vna0.get(k) is not None}      # start/stop are None in CW mode
                if not str(vna0.get("sweep_type", "LIN")).upper().startswith("LIN"):
                    restore["sweep_type"] = vna0["sweep_type"][:3]     # CW etc.
                mgr.apply("vna", owner, **restore)
                mgr.apply("vna", owner, output=bool(vna0.get("output")))
                done.append("VNA restored")
                if p["set_switches"] and "error" not in sw0:
                    mgr.apply("warm_switches", owner, **{k: sw0[k] for k in SWITCH_PATH if k in sw0})
                    done.append("switches restored")
            elif p["finish"] in ("rf_off", "off"):
                mgr.apply("vna", owner, output=False)
                done.append("VNA RF off")
            self.finish_last(p, job, snapshot, done)
            with mgr.use("vna", owner) as vna:
                vna.idle()          # continuous sweep + local, so the front panel works again
        except Exception as e:
            job.notes.append(f"finish step failed: {e}")
            log.error("%s: finish step failed: %s", self.id, e)
            return
        finally:
            if done:
                job.record["finish"] = done
