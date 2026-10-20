"""Driver tests against the simulators, using the real config.yaml entries."""
import numpy as np
import pytest

from labserver.config import load_config
from labserver.drivers import DRIVERS, InstrumentError
from labserver.drivers.zva import s_db
from labserver.sim import SimWorld

FAST = {"retry_delay_s": 0, "settle_after_read_s": 0, "ramp_delay_s": 0}


@pytest.fixture
def world():
    return SimWorld(time_scale=0)


@pytest.fixture
def lab(world):
    cfg = load_config()["instruments"]
    return {name: DRIVERS[c["driver"]].open(name, {**c, **FAST}, sim=True, world=world)
            for name, c in cfg.items()}


def test_every_configured_instrument_opens_and_reports_state(lab):
    for name, inst in lab.items():
        assert inst.idn(), name
        assert isinstance(inst.get_state(), dict), name
        assert inst.errors() == [], name


# ---- attenuator ----
def test_attenuator_set_and_readback(lab, world):
    assert lab["attenuator"].apply(attenuation_db=25)["attenuation_db"] == 25
    assert world.attenuation_db == 25


@pytest.mark.parametrize("bad,msg", [(25.1, "multiple"), (70, "outside limit")])
def test_attenuator_rejects(lab, bad, msg):
    with pytest.raises(InstrumentError, match=msg):
        lab["attenuator"].apply(attenuation_db=bad)


# ---- windfreak ----
def test_windfreak_channels(lab, world):
    s = lab["rf_synth"].apply(pump_enable=True, pump_frequency_hz=8.5e9, pump_power_dbm=7)
    assert (s["pump_enable"], s["pump_frequency_hz"], s["pump_power_dbm"]) == (True, 8.5e9, 7)
    assert "probe_enable" not in s          # channel A is not named: the probe is the SMW200A
    assert world.sources["rf_pump"]["on"] and not world.sources["rf_probe"]["on"]


def test_windfreak_limits_and_safe_off(lab, world):
    with pytest.raises(InstrumentError, match="outside limit"):
        lab["rf_synth"].apply(pump_power_dbm=15)
    with pytest.raises(InstrumentError, match="unknown parameter"):
        lab["rf_synth"].apply(probe_enable=True)
    lab["rf_synth"].apply(pump_enable=True)
    lab["rf_synth"].t[0].enable = True      # unnamed channel A left on (front panel, an old session)
    lab["rf_synth"].safe_off()
    assert not world.sources["rf_synth_a"]["on"] and not world.sources["rf_pump"]["on"]


def test_windfreak_device_range_errors_are_instrument_errors(lab):
    with pytest.raises(InstrumentError):
        lab["rf_synth"].apply(pump_frequency_hz=30e9)


# ---- SMW200A (probe) ----
def test_smw200a_probe_on_off_and_state(lab, world):
    s = lab["rf_probe"].apply(probe_frequency_hz=5.58e9, probe_power_dbm=-120.5, probe_enable=True)
    assert (s["probe_enable"], s["probe_frequency_hz"], s["probe_power_dbm"]) == (True, 5.58e9, -120.5)
    assert (s["probe_freq_mode"], s["probe_modulation"], s["probe_iq"], s["reference_source"]) == ("CW", False, False,
                                                                                                    "INT")
    assert world.sources["rf_probe"] == {"on": True, "frequency_hz": 5.58e9, "power_dbm": -120.5}
    lab["rf_probe"].safe_off()
    assert not world.sources["rf_probe"]["on"]


def test_smw200a_sets_frequency_and_level_before_rf_on(lab):
    sent = []
    write = lab["rf_probe"].t.write
    lab["rf_probe"].t.write = lambda cmd: (sent.append(cmd), write(cmd))[1]
    lab["rf_probe"].apply(probe_enable=True, probe_power_dbm=-30, probe_frequency_hz=6e9)
    assert sent[-1] == ":OUTP1:STAT ON" and len(sent) == 3
    sent.clear()
    lab["rf_probe"].apply(probe_frequency_hz=5e9, probe_enable=False)
    assert sent[0] == ":OUTP1:STAT OFF"


def test_smw200a_limits_and_range(lab):
    with pytest.raises(InstrumentError, match="outside limit"):
        lab["rf_probe"].apply(probe_power_dbm=5)
    with pytest.raises(InstrumentError, match="SMW200A range"):
        lab["rf_probe"].apply(probe_frequency_hz=50e9)
    with pytest.raises(InstrumentError, match="unknown parameter"):
        lab["rf_probe"].apply(pump_enable=True)


def test_windfreak_does_not_touch_reference_on_connect(lab):
    assert lab["rf_synth"].get_state()["reference_mode"] == "external"


# ---- GS210 (qubit flux bias) ----
def test_gs210_flux_bias_in_one_apply(lab, world):
    s = lab["qubit_flux_bias"].apply(output=True, level=-0.26, range=1, function="VOLT")
    assert (s["range"], s["level"], s["output"]) == (1, -0.26, True)
    assert world.flux_bias_v == -0.26
    lab["qubit_flux_bias"].safe_off()
    assert world.flux_bias_v == 0


def test_gs210_turns_off_before_changing_level(lab):
    gs = lab["qubit_flux_bias"]
    gs.apply(range=1, level=-0.26, output=True)
    writes = []
    orig = gs.t.write
    gs.t.write = lambda cmd: (writes.append(cmd), orig(cmd))
    gs.apply(level=0, output=False)
    assert writes[0] == ":OUTP OFF"


def test_gs210_ramps_while_output_on(lab):
    gs = lab["qubit_flux_bias"]
    gs.apply(range=1, output=True)
    levels = []
    orig = gs.t.write
    gs.t.write = lambda cmd: (levels.append(float(cmd.split()[1])) if "LEV" in cmd else None, orig(cmd))
    gs.apply(level=-0.26)
    assert levels[-1] == -0.26 and len(levels) == 6
    assert max(abs(np.diff([0.0] + levels))) <= 0.05 + 1e-12


def test_gs210_limit(lab):
    with pytest.raises(InstrumentError, match="outside limit"):
        lab["qubit_flux_bias"].apply(level=1.5)


def test_gs210_refuses_to_output_a_leftover_level(lab, world):
    gs = lab["qubit_flux_bias"]
    gs.t.s.update(rng=30.0, lev=28.0)        # e.g. left at noise-diode settings
    with pytest.raises(InstrumentError, match="outside limit"):
        gs.apply(output=True)
    assert world.yoko_v == 0


def test_flux_bias_moves_qubit(lab, world):
    lab["warm_switches"].apply(fridge_in="vna", rx_source="fridge", receiver="vna")
    lab["qubit_flux_bias"].apply(range=1, level=-0.26, output=True)
    assert world.qubit_hz() == pytest.approx(5.346e9, rel=1e-3)
    lab["vna"].apply(start_hz=5.30e9, stop_hz=5.40e9, ifbw_hz=10, power_dbm=-50, output=True)
    tr = lab["vna"].read_trace()
    assert tr.x[np.argmin(s_db(tr.y))] == pytest.approx(world.qubit_hz(), abs=1e6)


# ---- 33220A ----
def test_33220a_dc_bias(lab, world):
    s = lab["noise_source_bias"].apply(function="DC", offset_v=0.2, output=True)
    assert (s["function"], s["offset_v"], s["output"]) == ("DC", 0.2, True)
    assert world.noise_source_v == 0.2
    lab["noise_source_bias"].safe_off()
    assert world.noise_source_v == 0


# ---- ZVA ----
def test_zva_trace_shows_qubit_dip_when_routed(lab, world):
    lab["warm_switches"].apply(fridge_in="vna", rx_source="fridge", receiver="vna")
    lab["vna"].apply(start_hz=5.53e9, stop_hz=5.63e9, points=401, ifbw_hz=10, power_dbm=-50, output=True)
    tr = lab["vna"].read_trace()
    assert np.iscomplexobj(tr.y) and len(tr.y) == 401 and tr.x_unit == "Hz"
    db = s_db(tr.y)
    assert tr.x[np.argmin(db)] == pytest.approx(5.58e9, abs=1e6)
    assert np.median(db) - db.min() > 3
    assert lab["vna"].get_state()["output"] is True


def test_zva_high_power_saturates_qubit(lab):
    lab["warm_switches"].apply(fridge_in="vna", rx_source="fridge", receiver="vna")
    lab["vna"].apply(start_hz=5.53e9, stop_hz=5.63e9, ifbw_hz=10, output=True)
    depth = {}
    for p in (-50, -10):
        lab["vna"].apply(power_dbm=p)
        db = s_db(lab["vna"].read_trace().y)
        depth[p] = np.median(db) - db.min()
    assert depth[-10] < depth[-50] / 3


def test_zva_stays_single_sweep_until_idle(lab):
    vna = lab["vna"]
    vna.read_trace()
    vna.read_trace()
    assert vna.t.s["cont"] is False     # no remote/local switch per trace (screen flashed)
    vna.idle()
    assert vna.t.s["cont"] is True


def test_zva_keeps_display_updating_in_remote(lab):
    assert lab["vna"].t.s["disp_update"] is True


def test_zva_cw_mode_has_time_axis(lab):
    lab["vna"].apply(sweep_type="CW", cw_hz=5.58e9)
    assert lab["vna"].read_trace().x_unit == "s"


def test_zva_state_in_cw_mode_skips_start_stop(lab):
    """The real ZVA doesn't answer FREQ:STARt?/STOP? in CW mode; reading them broke
    GET /api/vna/state and every apply (2026-10-14)."""
    vna = lab["vna"]
    vna.apply(sweep_type="CW", cw_hz=5.576e9)
    st = vna.apply(power_dbm=-20, output=True)
    assert st["start_hz"] is None and st["stop_hz"] is None
    assert st["cw_hz"] == pytest.approx(5.576e9) and st["power_dbm"] == -20
    assert vna.t.query("SYST:ERR?").startswith("+0")       # nothing left in the error queue
    assert len(vna.read_trace().y) == st["points"]
    st = vna.apply(sweep_type="LIN")
    assert st["start_hz"] == pytest.approx(5.525e9)


def test_zva_sweep_type_change_with_start_stop(lab):
    """The UI sends start/stop before sweep_type; the ZVA refused FREQ:STARt while still
    in CW, so the VNA could not leave CW (2026-10-14)."""
    vna = lab["vna"]
    vna.apply(sweep_type="CW", cw_hz=5.6e9)
    with pytest.raises(InstrumentError):
        vna.apply(start_hz=4e9)                         # the sim refuses it in CW, like the ZVA
    st = vna.apply(start_hz=4e9, stop_hz=12e9, sweep_type="LIN")
    assert st["sweep_type"] == "LIN"
    assert (st["start_hz"], st["stop_hz"]) == (pytest.approx(4e9), pytest.approx(12e9))
    st = vna.apply(sweep_type="CW", start_hz=5e9, stop_hz=6e9, cw_hz=5.58e9)   # into CW: span first
    assert st["sweep_type"] == "CW" and st["cw_hz"] == pytest.approx(5.58e9)
    assert vna.apply(sweep_type="LIN")["start_hz"] == pytest.approx(5e9)


def test_zva_retries_injected_failures(lab, world):
    world.fail_rate = 0.5
    for _ in range(5):
        assert len(lab["vna"].read_trace().y) == 401


# ---- E7405A ----
def test_e7405a_scale_is_separate_from_unit(lab):
    """The E7405A changes the unit with the scale (the sim too); the driver puts it back."""
    spa = lab["spa"]
    st = spa.apply(scale="LIN", unit="W")
    assert st["scale"] == "LIN" and st["unit"] == "W"
    assert spa.t.query("DISP:WIND:TRAC:Y:SPAC?").strip() == "LIN"
    st = spa.apply(scale="log")
    assert st["scale"] == "LOG" and st["unit"] == "W"
    assert spa.read_trace().meta["scale"] == "LOG"
    spa.apply(unit="DBM")
    assert spa.apply(scale="LIN")["unit"] == "DBM"


def test_e7405a_rejects_unknown_scale(lab):
    with pytest.raises(InstrumentError, match="LOG or LIN"):
        lab["spa"].apply(scale="DB")
    assert lab["spa"].get_state()["scale"] == "LOG"        # nothing was sent


# ---- cross-instrument physics (what the jobs will rely on) ----
def zero_span_power(lab):
    lab["spa"].configure_zero_span(5.58e9, 1e6, 1e3)
    return lab["spa"].measure_power()["mean_w"]


def test_hot_wire_raises_spa_noise(lab):
    off = zero_span_power(lab)
    lab["noise_source_bias"].apply(function="DC", offset_v=0.2, output=True)
    assert zero_span_power(lab) > 1.2 * off


def test_noise_diode_seen_only_through_switch(lab, world):
    world.yoko_cabling = "noise_diode_bias"
    gs = lab["qubit_flux_bias"]
    gs.limits = {"level": [0.0, 30.0]}       # config change that goes with the recabling
    gs.apply(range=30, level=28, output=True)
    fridge = zero_span_power(lab)
    lab["warm_switches"].apply(rx_source="noise_diode")
    assert zero_span_power(lab) > 5 * fridge
    lab["warm_switches"].apply(receiver="vna")
    assert zero_span_power(lab) < 1e-12


def test_vna_cw_tone_reaches_spa(lab):
    off = zero_span_power(lab)
    lab["vna"].apply(sweep_type="CW", cw_hz=5.58e9, power_dbm=-10, output=True)
    assert zero_span_power(lab) > 2 * off


def test_twpa_pump_raises_noise_floor(lab):
    off = zero_span_power(lab)
    lab["rf_synth"].apply(pump_frequency_hz=8.5e9, pump_power_dbm=7, pump_enable=True)
    assert zero_span_power(lab) > 10 * off


def test_zva_expected_sweep_includes_sweep_count(lab):
    vna = lab["vna"]
    single = vna.expected_sweep_s()
    vna.t.write("SENS1:SWE:COUN 10")
    vna.forget_cached_state()
    assert vna.expected_sweep_s() == pytest.approx(10 * single)
    assert vna.get_state()["sweep_count"] == 10


def test_zva_recovers_from_front_panel_point_change(lab):
    vna = lab["vna"]
    vna.read_trace()                  # fills the settings cache
    vna.t.s["points"] = 201           # changed on the front panel
    with pytest.raises(Exception, match="expected 401"):
        vna.read_trace()
    assert len(vna.read_trace().y) == 201


def test_zva_set_power_updates_trace_meta(lab):
    vna = lab["vna"]
    vna.read_trace()
    vna.set_power(-40)
    assert vna.read_trace().meta["power_dbm"] == -40
