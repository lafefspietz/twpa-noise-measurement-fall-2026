"""Step-by-step hardware check for one instrument. Run this at the lab first.

    python scripts/bringup.py spa                 # real hardware
    python scripts/bringup.py spa --sim           # rehearse against the simulator
    python scripts/bringup.py all --sim           # every instrument in config.yaml
    python scripts/bringup.py spa --zero-span     # also apply notebook zero-span settings
    python scripts/bringup.py warm_switches --exercise   # cycle every switch, then restore
    python scripts/bringup.py vna -v              # print every command sent

Every instrument goes through: open -> *IDN? -> error queue -> state -> re-apply the
current values (checks setter syntax without changing anything) -> state unchanged.
Then instrument-specific checks (trace timing for SA/VNA). Outputs are never turned on.
Each step prints [ OK ] or [FAIL].
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labserver.config import load_config  # noqa: E402
from labserver.drivers import DRIVERS  # noqa: E402
from labserver.sim import SimWorld  # noqa: E402

failures = 0

# Parameters that are safe to write back with their current value.
# Output on/off and GS210 function are deliberately not here.
REAPPLY = {
    "e7405a": ["center_hz", "span_hz", "rbw_hz", "vbw_hz", "detector", "ref_level", "unit",
               "attenuation_db", "average_type"],
    "zva": ["start_hz", "stop_hz", "points", "ifbw_hz", "power_dbm", "sweep_type", "cw_hz"],
    "windfreak": [f"{ch}_{p}" for ch in ("probe", "pump") for p in ("frequency_hz", "power_dbm")],
    "gs210": ["range", "level"],
    "fgen33220a": ["function", "offset_v"],
    "rcdat": ["attenuation_db"],
    "rc4spdt": None,   # all named switches
}


def step(title, fn):
    global failures
    print(f"\n--- {title}")
    try:
        result = fn()
        print("[ OK ]", "" if result is None else result)
        return result
    except Exception as e:
        failures += 1
        print(f"[FAIL] {type(e).__name__}: {e}")
        if args.verbose:
            traceback.print_exc()
        return None


def time_reads(inst, n):
    t0 = time.perf_counter()
    for _ in range(n):
        tr = inst.read_trace()
    dt = (time.perf_counter() - t0) / n
    return tr, f"{dt * 1000:.1f} ms/trace ({1 / dt:.1f} fps), {len(tr.y)} points, x in {tr.x_unit}"


def reapply(inst, driver_name):
    before = inst.get_state()
    keys = REAPPLY[driver_name]
    if keys is None:
        keys = list(inst.switches)
    params = {k: before[k] for k in keys if k in before}
    after = inst.apply(**params)
    changed = {k: (before[k], after[k]) for k in params if before[k] != after[k]}
    if changed:
        raise RuntimeError(f"values changed on re-apply (before, after): {changed}")
    return f"re-sent {sorted(params)}"


# ---- instrument-specific checks ----
def extra_e7405a(inst):
    if args.zero_span:
        step("configure zero span (notebook: 5.58 GHz, RBW 1 MHz, VBW 1 kHz, SAMP)",
             lambda: inst.configure_zero_span(5.58e9, 1e6, 1e3) or inst.get_state())
    step(f"trace x{args.n}", lambda: time_reads(inst, args.n)[1])
    if inst.get_state()["span_hz"] == 0:
        step("measure_power (zero span)", inst.measure_power)
    else:
        print("\n(span != 0, skipping measure_power; use --zero-span)")


def extra_zva(inst):
    def read():
        tr, msg = time_reads(inst, args.n)
        db = 20 * np.log10(np.abs(tr.y))
        return f"{msg}, |S21| median {np.median(db):.1f} dB"
    step(f"trace x{args.n} (FORM REAL,32 / CALC1:DATA? SDAT, output state unchanged)", read)


def extra_rc4spdt(inst):
    if not args.exercise:
        print("\n(use --exercise to move each switch through every position and restore)")
        return
    original = inst.get_state()
    for sw, spec in inst.switches.items():
        for pos in spec["positions"]:
            step(f"{sw} -> {pos}", lambda: inst.apply(**{sw: pos})[sw])
    step("restore original positions", lambda: inst.apply(**{sw: original[sw] for sw in inst.switches}))


EXTRA = {"e7405a": extra_e7405a, "zva": extra_zva, "rc4spdt": extra_rc4spdt}


def bringup(name, icfg, rm, world):
    driver_name = icfg["driver"]
    print(f"\n========== {name} ({driver_name}) ==========")
    opened = {}
    step("open", lambda: opened.setdefault("inst", DRIVERS[driver_name].open(
        name, icfg, sim=args.sim, world=world, rm=rm)).t.resource_name)
    inst = opened.get("inst")
    if inst is None:
        return
    step("identify", inst.idn)
    step("error queue (drain)", lambda: inst.errors() or "empty")
    step("state", inst.get_state)
    if not args.no_reapply:
        step("re-apply current values", lambda: reapply(inst, driver_name))
    if driver_name in EXTRA:
        EXTRA[driver_name](inst)
    step("error queue after test", lambda: inst.errors() or "empty")
    step("close", inst.close)


parser = argparse.ArgumentParser()
parser.add_argument("instrument", help="key from config.yaml (e.g. spa), or 'all'")
parser.add_argument("--sim", action="store_true")
parser.add_argument("--zero-span", action="store_true")
parser.add_argument("--exercise", action="store_true", help="switches: cycle every position, then restore")
parser.add_argument("--no-reapply", action="store_true", help="read only, send no settings at all")
parser.add_argument("-n", type=int, default=10, help="traces per timing test")
parser.add_argument("-v", "--verbose", action="store_true")
parser.add_argument("--config")
args = parser.parse_args()

logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(name)s %(message)s")
cfg = load_config(args.config)
names = list(cfg["instruments"]) if args.instrument == "all" else [args.instrument]

rm = None
if not args.sim:
    import pyvisa
    import serial.tools.list_ports

    from labserver import usbtmc_patch
    usbtmc_patch.apply()

    rm = step("VISA resource manager", lambda: pyvisa.ResourceManager(cfg.get("visa_library", "")))
    if rm is not None:
        step("VISA resources", lambda: "\n       " + "\n       ".join(rm.list_resources()))
    step("COM ports", lambda: "\n       " + "\n       ".join(
        f"{p.device}  {p.hwid}" for p in serial.tools.list_ports.comports()))

world = SimWorld.from_config(cfg["sim"])
for name in names:
    bringup(name, cfg["instruments"][name], rm, world)

print(f"\n{'ALL OK' if not failures else f'{failures} step(s) FAILED'}")
sys.exit(1 if failures else 0)
