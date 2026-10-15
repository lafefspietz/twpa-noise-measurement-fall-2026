import numpy as np
import pytest

from labserver.drivers import E7405A, InstrumentError, MeasurementError
from labserver.sim import SimWorld
from labserver.transport import TransportError

CFG = {"retry_delay_s": 0, "settle_after_read_s": 0, "align_poll_s": 0}


@pytest.fixture
def world():
    return SimWorld(time_scale=0)


@pytest.fixture
def spa(world):
    return E7405A.open("spa", CFG, sim=True, world=world)


def test_idn(spa):
    assert "E7405A" in spa.idn()


def test_zero_span_settings_reach_state(spa):
    spa.configure_zero_span(5.58e9, 1e6, 1e3)
    s = spa.get_state()
    assert (s["center_hz"], s["span_hz"], s["rbw_hz"], s["vbw_hz"], s["detector"]) == (5.58e9, 0, 1e6, 1e3, "SAMP")


def test_measure_power_matches_notebook_level(spa):
    spa.configure_zero_span(5.58e9, 1e6, 1e3)
    spa.apply(unit="W")
    r = spa.measure_power()
    assert r["mean_w"] == pytest.approx(1.15e-9, rel=0.02)
    assert r["n_points"] == 401


def test_measure_power_converts_dbm_to_watts(spa):
    spa.configure_zero_span(5.58e9, 1e6, 1e3)
    spa.apply(unit="DBM")
    assert spa.measure_power()["mean_w"] == pytest.approx(1.15e-9, rel=0.02)


def test_measure_power_requires_zero_span(spa):
    spa.apply(span_hz=1e6)
    with pytest.raises(MeasurementError):
        spa.measure_power()


def test_frequency_trace_shows_tone(spa, world):
    world.tones["probe"] = (5.58e9, 1e-6)
    spa.apply(center_hz=5.58e9, span_hz=10e6, rbw_hz=100e3)
    tr = spa.read_trace()
    assert tr.x_unit == "Hz" and tr.x[0] == pytest.approx(5.575e9)
    assert tr.x[np.argmax(tr.y)] == pytest.approx(5.58e9, abs=50e3)


def test_retries_through_injected_failures(world):
    world.fail_rate = 0.5
    spa = E7405A.open("spa", CFG, sim=True, world=world)
    for _ in range(10):
        assert not np.isnan(spa.read_trace().y).any()


def test_gives_up_after_max_retries(world):
    world.fail_rate = 1.0
    spa = E7405A.open("spa", {**CFG, "max_retries": 3}, sim=True, world=world)
    with pytest.raises(MeasurementError):
        spa.read_trace()


def test_apply_rejects_unknown_parameter(spa):
    with pytest.raises(InstrumentError, match="unknown parameter"):
        spa.apply(frequency=1e9)


def test_apply_surfaces_instrument_errors(spa):
    with pytest.raises(InstrumentError, match="Illegal parameter"):
        spa.apply(detector="BOGUS")


def test_single_sweep_mode_while_measuring_then_continuous_on_close(spa):
    assert spa.get_state()["continuous"] is True
    spa.read_trace()
    assert spa.get_state()["continuous"] is False
    spa.close()
    assert spa.t.s["cont"] is True


def test_single_sweep_can_be_disabled(world):
    spa = E7405A.open("spa", {**CFG, "single_sweep": False}, sim=True, world=world)
    spa.read_trace()
    assert spa.get_state()["continuous"] is True


def test_limits_from_config(world):
    spa = E7405A.open("spa", {**CFG, "limits": {"ref_level": [-100, 10]}}, sim=True, world=world)
    with pytest.raises(InstrumentError, match="outside limit"):
        spa.apply(ref_level=30)


def test_sim_rounds_bandwidths_like_the_esa(spa):
    s = spa.apply(rbw_hz=1.5e3, vbw_hz=200)
    assert (s["rbw_hz"], s["vbw_hz"]) == (1e3, 300)


def test_sim_couples_bandwidths_to_span_while_auto(spa):
    s = spa.apply(span_hz=10e6)                   # auto: RBW ~ span/106 -> 100 kHz, VBW follows
    assert (s["rbw_hz"], s["vbw_hz"]) == (100e3, 100e3)
    s = spa.apply(rbw_hz=30e3)                    # manual RBW: auto off for RBW, VBW still follows
    assert (s["rbw_hz"], s["vbw_hz"]) == (30e3, 30e3)
    s = spa.apply(span_hz=100e6)
    assert s["rbw_hz"] == 30e3


def test_expected_sweep_includes_averaging(spa):
    single = spa.expected_sweep_s()
    spa.t.write(":AVER:COUN 50")      # e.g. set on the front panel
    spa.t.write(":AVER ON")
    assert spa.expected_sweep_s() == single          # cached until told otherwise
    spa.forget_cached_state()
    assert spa.expected_sweep_s() == pytest.approx(50 * single)
    s = spa.get_state()
    assert (s["average"], s["average_count"]) == (True, 50)


def test_set_unit_updates_trace_unit(spa):
    spa.read_trace()                  # fills the settings cache
    spa.set_unit("W")
    assert spa.read_trace().y_unit == "W"


def test_read_trace_retry_override(world):
    spa = E7405A.open("spa", {**CFG, "max_retries": 50}, sim=True, world=world)
    world.fail_rate = 1.0
    with pytest.raises(MeasurementError, match="after 2 attempts"):
        spa.read_trace(max_retries=2)


def test_failed_reads_leave_no_errors_for_apply_without_device_clear(spa, monkeypatch):
    """pyvisa-py USB has no clear(); errors left by failed reads must still be drained,
    or a later apply() reports them as its own (seen on hardware 2026-10-13)."""
    def no_clear():
        raise TransportError("clear: VI_ERROR_NSUP_OPER")

    def failing_fetch():
        for code, msg in ((-420, "Query UNTERMINATED"), (-102, "Syntax error"), (-113, "Undefined header")):
            spa.t.push_error(code, msg)
        raise TransportError("'*OPC?': VI_ERROR_TMO")

    monkeypatch.setattr(spa.t, "clear", no_clear)
    real_fetch = spa._fetch
    monkeypatch.setattr(spa, "_fetch", failing_fetch)
    for _ in range(10):                              # 30 errors: more than one 20-read drain
        with pytest.raises(MeasurementError):
            spa.read_trace(max_retries=1)
    monkeypatch.setattr(spa, "_fetch", real_fetch)
    assert spa.apply(center_hz=4e9)["center_hz"] == 4e9


def test_unanswered_optional_query_falls_back_once(spa):
    sent = []
    query = spa.t.query
    spa.t.query = lambda cmd: (sent.append(cmd), query(cmd))[1]
    assert spa.query_optional(":BOGUS?", "7") == "7"
    assert spa.query_optional(":BOGUS?", "7") == "7"
    assert sent.count(":BOGUS?") == 1                # not sent again
    assert spa.errors() == []                        # error queue drained, next apply() is clean


def test_zero_span_has_no_auto_sweep_time(spa):
    """As our E7405A: going to zero span keeps the last sweep time as a manual value, and
    asking for auto there is error +620."""
    spa.apply(span_hz=1e6)
    assert spa.get_state()["sweep_time_auto"] is True
    st = spa.apply(span_hz=0)
    assert st["sweep_time_auto"] is False and st["sweep_time_s"] > 0
    with pytest.raises(InstrumentError, match="620"):
        spa.apply(sweep_time_s=None)
    assert spa.apply(sweep_time_s=0.2)["sweep_time_s"] == 0.2
    st = spa.apply(span_hz=1e6, sweep_time_s=None)
    assert st["sweep_time_auto"] is True


# ---- alignment ----
def test_decode_alignment_bits():
    from labserver.drivers.e7405a import decode_alignment
    st = decode_alignment(6152)             # what ours reported after :CAL:ALL without the cable
    assert st["align_rf_needed"] and st["align_rf_skipped"] and not st["align_needed"]
    assert st["failed"] == []               # the RF "failure" is the skip, not a fault
    st = decode_alignment((1 << 14) | (1 << 4))
    assert st["align_needed"] and st["failed"] == ["IF"]
    assert decode_alignment(-1)["known"] is False


def test_state_has_alignment_status(spa):
    assert spa.get_state()["alignment"] == {"cal_condition": 0, "known": True, "align_needed": False,
                                            "align_rf_needed": False, "align_rf_skipped": False, "failed": []}


def test_align_without_cable_skips_rf(spa):
    steps = []
    res = spa.align(progress=steps.append)
    assert steps                                    # progress was reported while it ran
    assert res["rf_skipped_no_cable"] is True and res["errors"] == []      # +652 is expected, not an error
    assert res["after"]["align_rf_needed"] and res["after"]["failed"] == []
    assert spa.t.query("SYST:ERR?").startswith("+0")                     # queue drained
    assert len(spa.read_trace().y) == 401           # the SA reads again afterwards
