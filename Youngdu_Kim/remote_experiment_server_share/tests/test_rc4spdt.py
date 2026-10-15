import pytest

from labserver.config import load_config
from labserver.drivers import RC4SPDT, InstrumentError
from labserver.drivers.rc4spdt import decode_ports, encode_ports
from labserver.sim import SimWorld


@pytest.fixture
def world():
    w = SimWorld(time_scale=0)
    w.switch_ports = dict.fromkeys("ABCD", 1)
    return w


@pytest.fixture
def sw(world):
    cfg = load_config()["instruments"]["warm_switches"]
    return RC4SPDT.open("warm_switches", cfg, sim=True, world=world)


def test_byte_encoding_matches_notebook():
    # set_warm_switches_by_ip(ip, 1, 2, 2, 1) -> 128 + 2 + 4
    assert encode_ports({"A": 1, "B": 2, "C": 2, "D": 1}) == 134
    assert decode_ports("134") == {"A": 1, "B": 2, "C": 2, "D": 1}


def test_named_state(sw):
    assert sw.get_state() == {"fridge_in": "vna", "rx_source": "fridge", "receiver": "vna",
                              "ports": {"A": 1, "B": 1, "C": 1, "D": 1}}


def test_switches_move_independently(sw, world):
    sw.apply(receiver="spa")
    assert world.switch_ports == {"A": 1, "B": 2, "C": 1, "D": 1}
    sw.apply(rx_source="noise_diode")
    assert world.switch_ports == {"A": 1, "B": 2, "C": 2, "D": 1}   # B untouched


def test_notebook_paths(sw, world):
    # synth --> fridge --> spa  ==  (1, 2, 1, 2)
    state = sw.apply(fridge_in="rf_probe", rx_source="fridge", receiver="spa")
    assert state["ports"] == {"A": 1, "B": 2, "C": 1, "D": 2}


def test_unused_channel_a_is_preserved(sw, world):
    world.switch_ports["A"] = 2
    sw.apply(receiver="spa")
    assert world.switch_ports["A"] == 2


def test_rejects_unknown_switch_and_position(sw):
    with pytest.raises(InstrumentError, match="unknown switch"):
        sw.apply(A=2)
    with pytest.raises(InstrumentError, match="unknown position"):
        sw.apply(receiver="scope")


def test_readback_mismatch_is_an_error(sw, world, monkeypatch):
    monkeypatch.setattr(sw.t, "get", lambda path: "1" if path.startswith(":SETP") else "128")
    with pytest.raises(InstrumentError, match="switch reports"):
        sw.apply(receiver="spa")
