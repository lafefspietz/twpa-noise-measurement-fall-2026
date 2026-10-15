import pytest

from labserver.sim.scpi import compile_pattern, matches, parse_number, split_command


@pytest.mark.parametrize("cmd", [
    ":SENS:BAND?", "SENSe:BANDwidth?", ":BAND 1e6", "band:res 1e6", ":SENSE:BANDWIDTH:RESOLUTION?",
])
def test_optional_nodes_and_short_long_forms(cmd):
    tokens, _, _ = split_command(cmd)
    assert matches(compile_pattern("[SENSe]:BANDwidth[:RESolution]"), tokens)


def test_video_bandwidth_does_not_match_rbw():
    tokens, _, _ = split_command(":SENS:BAND:VID?")
    assert not matches(compile_pattern("[SENSe]:BANDwidth[:RESolution]"), tokens)
    assert matches(compile_pattern("[SENSe]:BANDwidth:VIDeo"), tokens)


def test_numeric_suffix_is_ignored():
    tokens, is_query, args = split_command("SOURce1:POWer -35")
    assert tokens == ["SOURCE", "POWER"] and not is_query and args == "-35"
    assert matches(compile_pattern("SOURce:POWer"), tokens)


def test_split_query_with_args():
    assert split_command("TRAC? TRACE1") == (["TRAC"], True, "TRACE1")


@pytest.mark.parametrize("arg,value", [("5.58 GHz", 5.58e9), ("1e6", 1e6), ("10MS", 0.01), ("-30", -30.0)])
def test_parse_number(arg, value):
    assert parse_number(arg) == pytest.approx(value)
