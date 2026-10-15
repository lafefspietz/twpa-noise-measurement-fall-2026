"""Minimal SCPI header matching for simulators.

Patterns are written the way manuals write them, e.g. "[SENSe]:BANDwidth[:RESolution]":
uppercase letters are the short form, [] marks optional nodes. A header matches if each
node is given as its short or long form (case-insensitive), with optional nodes omitted.
"""
from __future__ import annotations

import re

_NODE = re.compile(r"\[:?([A-Za-z*]+)\]|:?([A-Za-z*]+)")
_UNITS = {
    "HZ": 1.0, "KHZ": 1e3, "MHZ": 1e6, "GHZ": 1e9,
    "S": 1.0, "MS": 1e-3, "US": 1e-6, "NS": 1e-9,
    "V": 1.0, "MV": 1e-3, "A": 1.0, "MA": 1e-3,
    "DBM": 1.0, "DB": 1.0,
}


def compile_pattern(pattern: str) -> list[tuple[str, str, bool]]:
    nodes = []
    for m in _NODE.finditer(pattern):
        mnem, optional = (m.group(1), True) if m.group(1) else (m.group(2), False)
        short = "".join(c for c in mnem if c.isupper() or c == "*")
        nodes.append((short, mnem.upper(), optional))
    return nodes


def split_command(cmd: str) -> tuple[list[str], bool, str]:
    """'TRAC? TRACE1' -> (['TRAC'], True, 'TRACE1')"""
    cmd = cmd.strip()
    header, _, args = cmd.partition(" ")
    is_query = header.endswith("?")
    header = header.rstrip("?").lstrip(":")
    tokens = [t.upper().rstrip("0123456789") for t in header.split(":") if t]
    return tokens, is_query, args.strip()


def matches(nodes, tokens) -> bool:
    if not nodes:
        return not tokens
    short, long, optional = nodes[0]
    if tokens and tokens[0] in (short, long) and matches(nodes[1:], tokens[1:]):
        return True
    return optional and matches(nodes[1:], tokens)


def parse_number(arg: str) -> float:
    """'5.58 GHz' / '5.58GHZ' / '1e6' -> float in base units."""
    m = re.fullmatch(r"\s*([-+0-9.eE]+)\s*([A-Za-z]*)\s*", arg)
    if not m:
        raise ValueError(f"not a number: {arg!r}")
    unit = m.group(2).upper()
    if unit and unit not in _UNITS:
        raise ValueError(f"unknown unit: {arg!r}")
    return float(m.group(1)) * _UNITS.get(unit, 1.0)


def parse_bool(arg: str) -> bool:
    a = arg.strip().upper()
    if a in ("1", "ON"):
        return True
    if a in ("0", "OFF"):
        return False
    raise ValueError(f"not a boolean: {arg!r}")
