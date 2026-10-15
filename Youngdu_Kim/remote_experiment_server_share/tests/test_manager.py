import copy
import threading

import pytest

from labserver.config import load_config
from labserver.drivers import InstrumentError
from labserver.manager import InstrumentBusy, InstrumentManager, InstrumentUnavailable, OutputsOff
from labserver.sim import SimWorld
from labserver.transport import TransportError


@pytest.fixture
def cfg():
    c = copy.deepcopy(load_config())
    for icfg in c["instruments"].values():
        icfg.update(retry_delay_s=0, settle_after_read_s=0, ramp_delay_s=0)
    c["off_lock_timeout_s"] = 0.2
    return c


@pytest.fixture
def world():
    return SimWorld(time_scale=0)


@pytest.fixture
def mgr(cfg, world):
    m = InstrumentManager(cfg, sim=True, world=world)
    m.connect_all()
    return m


def outputs_on(mgr):
    mgr.apply("rf_synth", pump_frequency_hz=8.5e9, pump_power_dbm=7, pump_enable=True, probe_enable=True)
    mgr.apply("vna", output=True)
    mgr.apply("qubit_flux_bias", range=1, level=-0.26, output=True)
    mgr.apply("noise_source_bias", function="DC", offset_v=0.2, output=True)


def all_off(world):
    return (not any(s["on"] for s in world.sources.values())
            and world.yoko_v == 0 and world.noise_source_v == 0)


def test_connects_everything(mgr):
    st = mgr.status()
    assert all(s["connected"] for s in st.values()), st
    assert "E7405A" in st["spa"]["idn"]


def test_one_bad_instrument_does_not_block_others(cfg, world):
    cfg["instruments"]["spa"]["driver"] = "no_such_driver"
    m = InstrumentManager(cfg, sim=True, world=world)
    result = m.connect_all()
    assert result["spa"] is False and result["vna"] is True
    assert "no_such_driver" in m.status()["spa"]["error"]
    with pytest.raises(InstrumentUnavailable):
        m.get_state("spa")
    cfg["instruments"]["spa"]["driver"] = "e7405a"
    assert m.connect("spa") and m.status()["spa"]["error"] is None


def test_busy_instrument_reports_owner(mgr):
    held, release = threading.Event(), threading.Event()

    def job():
        with mgr.use("spa", owner="job:noise_sweep"):
            held.set()
            release.wait(5)

    t = threading.Thread(target=job)
    t.start()
    held.wait(5)
    assert mgr.status()["spa"]["busy"] == "job:noise_sweep"
    with pytest.raises(InstrumentBusy, match="job:noise_sweep"):
        with mgr.use("spa", timeout=0.05):
            pass
    assert mgr.get_state("vna")                 # other instruments unaffected
    release.set()
    t.join()
    assert mgr.status()["spa"]["busy"] is None


def test_transport_errors_are_recorded(mgr, monkeypatch):
    def broken():
        raise TransportError("USB gone")
    with mgr.use("spa") as spa:
        monkeypatch.setattr(spa, "get_state", broken)
    with pytest.raises(TransportError):
        mgr.get_state("spa")
    assert "USB gone" in mgr.status()["spa"]["error"]


def test_driver_errors_pass_through(mgr):
    with pytest.raises(InstrumentError):
        mgr.apply("qubit_flux_bias", level=5)


def test_outputs_off_turns_everything_off_and_blocks_commands_until_end_halt(mgr, world):
    outputs_on(mgr)
    assert not all_off(world)
    results = mgr.outputs_off()
    assert all_off(world)
    assert results["rf_synth"] == "off"
    with pytest.raises(OutputsOff):
        mgr.apply("vna", output=True)
    mgr.end_halt()
    mgr.apply("vna", output=True)


def test_outputs_off_does_not_wait_for_a_stuck_job(mgr, world):
    outputs_on(mgr)
    held, release = threading.Event(), threading.Event()

    def stuck_job():
        with mgr.use("vna", owner="job:stuck"):
            held.set()
            release.wait(5)

    t = threading.Thread(target=stuck_job)
    t.start()
    held.wait(5)
    results = mgr.outputs_off()
    release.set()
    t.join()
    assert "forced" in results["vna"] and "job:stuck" in results["vna"]
    assert all_off(world)


def test_busy_instrument_does_not_delay_the_others(mgr, world):
    """Regression: the VNA in a long sweep held its lock and the stop reached the
    pump and flux bias only after the VNA's lock timeout."""
    mgr.off_lock_timeout_s = 2.0
    outputs_on(mgr)
    held, release = threading.Event(), threading.Event()

    def long_sweep():
        with mgr.use("vna", owner="live:vna"):
            held.set()
            release.wait(5)

    t = threading.Thread(target=long_sweep)
    t.start()
    held.wait(5)
    stopper = threading.Thread(target=mgr.outputs_off)
    stopper.start()
    try:
        assert wait_until(lambda: not world.sources["rf_pump"]["on"] and world.yoko_v == 0, 0.5)
        assert world.sources["vna"]["on"]          # still waiting for its lock
    finally:
        release.set()
        t.join()
        stopper.join()
    assert all_off(world)


def wait_until(cond, timeout):
    import time
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_shutdown_keeps_outputs(mgr, world):
    outputs_on(mgr)
    mgr.shutdown()
    assert world.sources["rf_pump"]["on"] and world.yoko_v == -0.26
    assert not any(s["connected"] for s in mgr.status().values())
    mgr.end_halt()
    assert mgr.halt.is_set()        # stays set after a shutdown


def test_connect_does_not_touch_outputs(cfg, world):
    """Spy on every command sent while connecting: none may switch an output."""
    sent = []
    from labserver.sim.transport import SimTransport
    orig = SimTransport._dispatch

    def spy(self, cmd):
        sent.append(cmd.upper())
        return orig(self, cmd)

    SimTransport._dispatch = spy
    try:
        InstrumentManager(cfg, sim=True, world=world).connect_all()
    finally:
        SimTransport._dispatch = orig
    assert sent
    assert not [c for c in sent if c.startswith(("OUTP", ":OUTP")) and "?" not in c]


def test_snapshot_of_busy_instrument_uses_last_state(mgr):
    """A live sweep longer than the snapshot wait must not drop the VNA settings."""
    mgr.apply("vna", ifbw_hz=10)
    held, release = threading.Event(), threading.Event()

    def live_sweep():
        with mgr.use("vna", owner="live:vna"):
            held.set()
            release.wait(5)

    t = threading.Thread(target=live_sweep)
    t.start()
    held.wait(5)
    try:
        snap = mgr.snapshot(timeout=0.05)
    finally:
        release.set()
        t.join(5)
    vna = snap["vna"]
    assert "error" not in vna and vna["ifbw_hz"] == 10 and "ZVA" in vna["idn"]
    assert "live:vna" in vna["cached"]["reason"] and vna["cached"]["age_s"] >= 0
    assert "cached" not in snap["spa"]                  # free instruments are read as before


def test_snapshot_of_busy_instrument_without_state_is_an_error(mgr):
    mgr.slots["vna"].inst.last_state = None
    held, release = threading.Event(), threading.Event()

    def live_sweep():
        with mgr.use("vna", owner="live:vna"):
            held.set()
            release.wait(5)

    t = threading.Thread(target=live_sweep)
    t.start()
    held.wait(5)
    try:
        snap = mgr.snapshot(timeout=0.05)
    finally:
        release.set()
        t.join(5)
    assert snap["vna"] == {"error": "InstrumentBusy: vna is busy (live:vna)"}
