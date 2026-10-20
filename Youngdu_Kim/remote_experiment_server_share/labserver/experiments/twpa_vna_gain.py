"""TWPA VNA gain: S21 over a frequency range with the TWPA pump off and on, for the TWPA
gain at every frequency, G = |S21(pump on) / S21(pump off)|^2.

A run repeats the two states in rounds, every other round in reverse order (off, on, then
on, off, ...), so the pump toggles once per round and slow drifts cancel between them.
Each state is the mean of single-sweep complex traces, averaged over all rounds; the gain
comes from the two means. Unlike the hot-wire step on the SA (TWPA noise spectrum), the
coherent S21 sees no idler: it is the gain of the TWPA alone.

Signal path: fridge_in -> vna, rx_source -> fridge, receiver -> vna (as the qubit sweeps).
"""
from __future__ import annotations

import logging
import time

import numpy as np

from ..drivers.base import within
from ..drivers.rcdat import STEP_DB as ATT_STEP_DB
from .base import Param
from .qubit_flux_sweep import FLUX, flux_finish, flux_setup, flux_start
from .vna_sweep import SWITCH_PATH, VnaMapSweep
from .y_factor import PUMP_KEYS, PUMP_ON, SYNTH, pump_text, synth_restore

log = logging.getLogger("labserver.experiments")

ATT = "attenuator"
STATES = ["off", "on"]          # pump, even rounds

ROUNDS = "Rounds"
VNA = "VNA"
PUMP = "TWPA pump"
QUBIT = "Qubit flux bias"
END = "Before and after"
SAVE = "Save"


def sweep_order(rounds: int) -> list[tuple[int, str]]:
    """[(round, pump)], every other round reversed."""
    return [(k, s) for k in range(rounds) for s in (STATES if k % 2 == 0 else STATES[::-1])]


def s21_db(s) -> list[float]:
    return np.round(20 * np.log10(np.maximum(np.abs(np.asarray(s, dtype=complex)), 1e-30)), 4).tolist()


class TwpaVnaGain(VnaMapSweep):
    id = "twpa_vna_gain"
    title = "TWPA VNA gain"
    summary = ("VNA S21 over a frequency range with the TWPA pump off and on, repeated in rounds: the TWPA gain "
               "|S21(on) / S21(off)|^2 at every frequency. Result: |S21| with the pump off and on, and the gain.")
    instruments = ["vna", "warm_switches", ATT, SYNTH, FLUX]
    turns_on = "the VNA RF output and the TWPA pump (and the flux bias, if set)"
    params = [
        Param("rounds", "rounds", kind="int", default=2, group=ROUNDS,
              help="Each round takes pump off and on; every other round goes the other way, so the pump toggles "
                   "once per round and slow drifts cancel."),
        Param("averages", "traces per state per round", kind="int", default=10, group=ROUNDS,
              help="Single-sweep complex traces, averaged."),
        Param("pump_settle_s", "wait after the pump goes on / off", unit="s", default=2.0, group=ROUNDS),
        # VNA
        Param("start_hz", "start", unit="GHz", scale=1e9, default=5.5e9, group=VNA),
        Param("stop_hz", "stop", unit="GHz", scale=1e9, default=6.5e9, group=VNA),
        Param("points", "points", kind="int", default=401, group=VNA,
              help="401 between 5.5 and 6.5 GHz: the same frequencies as the TWPA noise spectrum's defaults."),
        Param("ifbw_hz", "IFBW", unit="Hz", default=1e3, group=VNA),
        Param("power_dbm", "VNA power", unit="dBm", default=-10.0, group=VNA,
              help="Low enough that the TWPA stays linear with the pump on (check with the TWPA signal sweep's "
                   "power axis). Limit: vna power in the settings tab."),
        Param("att_mode", "warm step attenuator", kind="select", default="keep", group=VNA,
              options=[["keep", "leave it as it is"], ["set", "set it for the run"]],
              help="RCDAT between fridge_in and the fridge, in the VNA's path."),
        Param("attenuation_db", "attenuation", unit="dB", default=25.0, group=VNA,
              show_if={"att_mode": ["set"]}, from_state=[ATT, "attenuation_db"],
              help="Multiples of 0.25 dB. Limit: attenuator attenuation in the settings tab."),
        # pump
        Param("pump_frequency_hz", "pump frequency", unit="GHz", scale=1e9, default=8.05e9, group=PUMP,
              from_state=[SYNTH, "pump_frequency_hz"], help="Limit: rf_synth pump frequency in the settings tab."),
        Param("pump_power_dbm", "pump power", unit="dBm", default=7.0, group=PUMP,
              from_state=[SYNTH, "pump_power_dbm"], help="Limit: rf_synth pump power in the settings tab."),
        # qubit
        Param("flux_mode", "qubit flux bias", kind="select", default="set", group=QUBIT,
              options=[["set", "set it for the run"], ["keep", "leave it as it is"]],
              help="Use the same flux bias as the noise measurements. Either way it is reserved and its state saved."),
        Param("flux_bias_v", "flux bias", unit="V", default=0.0, group=QUBIT, show_if={"flux_mode": ["set"]},
              from_state=[FLUX, "level"], help="Limit: qubit_flux_bias level (VOLT mode) in the settings tab."),
        # before / after
        Param("set_switches", "set the warm switches to VNA -> fridge -> VNA first", kind="bool", default=True,
              group=END),
        Param("finish", "when done", kind="select", default="restore", group=END,
              options=[["restore", "restore the VNA, switches, attenuator, pump and flux bias as they were"],
                       ["last", "leave everything at the last state"],
                       ["off", "turn the VNA RF and the pump off, and the flux bias (ramped to 0 V) if the run set "
                               "it; the switches and attenuator stay"]]),
        # save
        Param("fridge_temperature_k", "fridge temperature (for the record)", unit="K", default=None, group=SAVE),
        Param("label", "label", kind="text", default="twpa-vna-gain", group=SAVE),
        Param("note", "note", kind="text", default="", group=SAVE),
    ]

    # ---- plan ----
    def plan(self, p: dict, mgr=None) -> dict:
        errors, warnings, notes = [], [], []
        p = {**p, "settle_s": 0.0}          # for check_vna
        self.check_vna(p, errors)
        if p["rounds"] < 1 or p["averages"] < 1:
            errors.append("rounds and traces per state must be >= 1")
        order = sweep_order(max(p["rounds"], 0))
        if len(order) * p["averages"] > self.cap("max_total_points"):
            errors.append(f"more than {self.cap('max_total_points')} traces in all; use fewer rounds or traces")
        if p["pump_settle_s"] < 0:
            errors.append("wait after the pump must be >= 0")
        self.check_limit(errors, "vna", "power_dbm", p["power_dbm"], "VNA power", "dBm")
        if p["att_mode"] == "set":
            self.check_limit(errors, ATT, "attenuation_db", p["attenuation_db"], "attenuation", "dB")
            if abs(p["attenuation_db"] / ATT_STEP_DB - round(p["attenuation_db"] / ATT_STEP_DB)) > 1e-9:
                errors.append(f"attenuation not a multiple of {ATT_STEP_DB:g} dB")
        self.check_limit(errors, SYNTH, "pump_frequency_hz", p["pump_frequency_hz"], "pump frequency", "Hz")
        self.check_limit(errors, SYNTH, "pump_power_dbm", p["pump_power_dbm"], "pump power", "dBm")
        if not 10e6 <= p["pump_frequency_hz"] <= 15e9:
            errors.append("pump frequency must be within 0.01..15 GHz (SynthHD)")
        if not -80 <= p["pump_power_dbm"] <= 20:
            errors.append("pump power must be within -80..20 dBm (SynthHD)")
        if p["start_hz"] <= p["pump_frequency_hz"] <= p["stop_hz"]:
            errors.append(f"the pump ({p['pump_frequency_hz'] / 1e9:g} GHz) is inside the VNA range; move the range")
        if p["flux_mode"] == "set":
            self.check_limit(errors, FLUX, "level", p["flux_bias_v"], "flux bias", "V")
        if mgr is not None:
            self.check_instruments(mgr, p, [], errors, warnings, notes)
        n = len(order) * p["averages"]
        estimate_s = n * self.trace_time_s(p) + (p["rounds"] + 1) * max(p["pump_settle_s"], 0)
        size_mb = (len(order) + 2) * 2 * p["points"] * 26 / 1e6
        if order and not errors:
            notes.append(f"{p['rounds']} rounds x 2 pump states x {p['averages']} = {n} traces")
        return {"points": [{"i": i, "round": k + 1, "pump": pump, "averages": p["averages"]}
                           for i, (k, pump) in enumerate(order)],
                "estimate_s": round(estimate_s, 1), "size_mb": round(size_mb, 2),
                "columns": [["i", "#", ""], ["round", "round", ""], ["pump", "pump", ""], ["averages", "traces", ""]],
                "errors": errors, "warnings": warnings, "notes": notes}

    def check_instruments(self, mgr, p, points, errors, warnings, notes):
        """Reads only."""
        super().check_instruments(mgr, p, points, errors, warnings, notes)
        try:
            synth = self.read_state(mgr, SYNTH)
            notes.append(f"TWPA pump is {pump_text(synth)}: set to {p['pump_frequency_hz'] / 1e9:g} GHz, "
                         f"{p['pump_power_dbm']:g} dBm and toggled each round"
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
                (errors if err else notes).append(err or note)
            else:
                notes.append("flux bias left as it is: " + (f"on at {fl['level']:g} V" if fl["output"] else "output off"))
        except Exception as e:
            (errors if p["flux_mode"] == "set" else warnings).append(f"could not read the flux bias: {e}")

    # ---- result ----
    @staticmethod
    def gain_axis(rec: dict) -> dict:
        ax = {"plot": "spectra", "spectra_label": "|S21| [dB]"}
        if "frequency_hz" in rec:
            ax.update(f=np.asarray(rec["frequency_hz"], dtype=float).tolist(),
                      title=(f"VNA {rec['vna_power_dbm']:g} dBm; pump {rec['pump_frequency_hz'] / 1e9:g} GHz "
                             f"{rec['pump_power_dbm']:g} dBm"),
                      spectra={f"pump {pump}": s21_db(np.asarray(rec[f"s21_{pump}_real"])
                                                      + 1j * np.asarray(rec[f"s21_{pump}_imag"]))
                               for pump in STATES if f"s21_{pump}_real" in rec},
                      derived={"gain_db": rec["gain_db"]} if "gain_db" in rec else {})
        return ax

    @staticmethod
    def gain_row(rec: dict, i: int) -> dict:
        return {k: rec[k][i] for k in ("round", "pump", "averages", "mean_db")}

    def result_view(self, rec: dict) -> dict:
        return {"axis": self.gain_axis(rec), "rows": [self.gain_row(rec, i) for i in range(len(rec["round"]))]}

    # ---- run ----
    def run(self, p: dict, job) -> None:
        plan = self.plan(p)
        if plan["errors"]:
            raise ValueError("; ".join(plan["errors"]))
        order = sweep_order(p["rounds"])
        mgr, owner = job.mgr, job.owner
        job.progress("reserving instruments", points_total=len(order), traces_total=len(order) * p["averages"])
        with mgr.claim(self.instruments, owner):
            snapshot = mgr.snapshot(owner=owner)
            for name in ("vna", SYNTH) + ((ATT,) if p["att_mode"] == "set" else ()) \
                    + ((FLUX,) if p["flux_mode"] == "set" else ()):
                if "error" in snapshot.get(name, {}):
                    raise RuntimeError(f"cannot read {name}: {snapshot[name]['error']}")
            freqs = np.linspace(p["start_hz"], p["stop_hz"], p["points"])
            rec = job.record
            rec.update(kind="experiment", experiment=self.id, params=self.recorded_params(p), note=p["note"],
                       sim=mgr.sim, fridge_temperature_k=p["fridge_temperature_k"], frequency_hz=freqs,
                       vna_power_dbm=p["power_dbm"], pump_frequency_hz=p["pump_frequency_hz"],
                       pump_power_dbm=p["pump_power_dbm"], round=[], pump=[], averages=[], point_start_time=[],
                       point_end_time=[], mean_db=[], s_real=[], s_imag=[], instruments=snapshot)
            job.axis = self.gain_axis(rec)
            try:
                self._measure(p, job, order, snapshot)
            finally:
                self._finish(p, job, snapshot)

    def _measure(self, p, job, order, snapshot):
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
                rec[key] = mgr.get_state(name, owner)
            except Exception as e:
                job.notes.append(f"could not read {name}: {e}")

        job.progress("setting the TWPA pump (off)")
        mgr.apply(SYNTH, owner, **{PUMP_ON: False})
        mgr.apply(SYNTH, owner, **{k: p[k] for k in PUMP_KEYS})
        setup = {"start_hz": p["start_hz"], "stop_hz": p["stop_hz"], "points": p["points"],
                 "ifbw_hz": p["ifbw_hz"], "power_dbm": p["power_dbm"]}
        if not str(snapshot["vna"].get("sweep_type", "LIN")).upper().startswith("LIN"):
            setup["sweep_type"] = "LIN"
        job.progress("setting up the VNA")
        state = mgr.apply("vna", owner, **setup)
        if state["points"] != p["points"]:
            raise RuntimeError(f"VNA took {state['points']} points, asked for {p['points']}")
        mgr.apply("vna", owner, output=True)

        sums = {pump: np.zeros(p["points"], dtype=complex) for pump in STATES}
        counts = dict.fromkeys(STATES, 0)
        pump_on, traces_done = False, 0
        for i, (k, pump) in enumerate(order):
            job.check()
            want_on = pump == "on"
            if want_on != pump_on or i == 0:
                job.progress(f"turning the TWPA pump {pump}")
                mgr.apply(SYNTH, owner, **{PUMP_ON: want_on})
                pump_on = want_on
                job.sleep(p["pump_settle_s"])
                if want_on and "pump_state" not in rec:
                    st = mgr.get_state(SYNTH, owner)
                    rec["pump_state"] = {key: st[key] for key in (PUMP_ON, *PUMP_KEYS, "pump_locked") if key in st}
                    if st.get("pump_locked") is False:
                        job.notes.append("the SynthHD pump channel is not locked")
            job.progress(f"round {k + 1}/{p['rounds']}: pump {pump}", points_done=i)
            t0 = time.time()
            total = np.zeros(p["points"], dtype=complex)
            for _ in range(p["averages"]):
                job.check()
                with mgr.use("vna", owner) as vna:
                    total += vna.read_trace().y
                traces_done += 1
                job.progress(traces_done=traces_done)
            mean = total / p["averages"]
            t1 = time.time()
            sums[pump] += mean
            counts[pump] += 1
            for name, value in (("round", k + 1), ("pump", pump), ("averages", p["averages"]),
                                ("point_start_time", round(t0, 3)), ("point_end_time", round(t1, 3)),
                                ("mean_db", round(10 * np.log10(np.mean(np.abs(mean) ** 2) + 1e-300), 4))):
                rec[name].append(value)
            rec["s_real"].append(mean.real)
            rec["s_imag"].append(mean.imag)
            s = sums[pump] / counts[pump]
            rec[f"s21_{pump}_real"], rec[f"s21_{pump}_imag"] = s.real, s.imag
            if counts["off"] and counts["on"]:
                off, on = sums["off"] / counts["off"], sums["on"] / counts["on"]
                with np.errstate(divide="ignore", invalid="ignore"):
                    g = 20 * np.log10(np.abs(on) / np.abs(off))
                rec["gain_db"] = [None if not np.isfinite(x) else round(float(x), 5) for x in g]
            job.axis = self.gain_axis(rec)
            job.add_row(self.gain_row(rec, i))
        job.progress(f"{len(order)} states measured", points_done=len(order))

    # ---- finish (VnaMapSweep._finish restores the VNA and the switches) ----
    def finish_more(self, p, job, snapshot, done):
        mgr, owner = job.mgr, job.owner
        if p["finish"] == "restore":
            if "error" not in snapshot.get(SYNTH, {}):
                synth_restore(self, job, snapshot[SYNTH], done)
            if p["flux_mode"] == "set":
                flux_finish(job, snapshot.get(FLUX, {}), "restore", self.limits(FLUX), done)
            att0 = snapshot.get(ATT, {})
            if p["att_mode"] == "set" and "error" not in att0:
                if within(self.limits(ATT).get("attenuation_db"), att0["attenuation_db"]):
                    mgr.apply(ATT, owner, attenuation_db=att0["attenuation_db"])
                    done.append("attenuator restored")
                else:
                    job.notes.append(f"attenuator not restored: the old value {att0['attenuation_db']:g} dB "
                                     f"is outside the limits")
        elif p["finish"] == "off":
            mgr.apply(SYNTH, owner, **{PUMP_ON: False})
            done.append("TWPA pump off")
            if p["flux_mode"] == "set":
                flux_finish(job, snapshot.get(FLUX, {}), "off", self.limits(FLUX), done)
