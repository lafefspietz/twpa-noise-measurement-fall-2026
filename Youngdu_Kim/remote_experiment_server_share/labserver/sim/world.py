"""Shared physical state for simulators.

Simulated instruments write their outputs here (VNA power, synth channels, bias
sources, switch positions) and read back what their inputs would see. The physics is
a cartoon with plausible magnitudes, not a calibrated model: enough that switch
routing, TWPA pump, noise sources and VNA power visibly change what the SA and VNA
measure, so jobs and the UI can be exercised end to end.

    fridge_in (D): vna | rf_probe -> attenuator -> fridge (atten, qubit, TWPA, amps) -> rx_source (C)
    rx_source (C): fridge | noise_diode -> receiver (B): vna | spa
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


def dbm_to_w(dbm):
    return 10 ** (np.asarray(dbm, dtype=float) / 10) * 1e-3


@dataclass
class SimWorld:
    seed: int = 1
    time_scale: float = 1.0     # multiplies every simulated delay; 0 = instant
    fail_rate: float = 0.0      # probability a trace read fails

    # ---- outputs written by instrument sims ----
    # warm RF switch box: channel -> port. Default: vna -> fridge -> spa
    switch_ports: dict[str, int] = field(default_factory=lambda: {"A": 1, "B": 2, "C": 1, "D": 1})
    # RF sources: name -> {"frequency_hz", "power_dbm", "on"}; names: vna, rf_probe, rf_pump
    sources: dict[str, dict] = field(default_factory=dict)
    noise_source_v: float = 0.0     # 33220A DC offset actually applied (0 when output off)
    yoko_v: float = 0.0             # GS210 voltage actually applied (0 when output off)
    # Where the GS210 is physically cabled: "qubit_flux_bias" (normal) or "noise_diode_bias"
    yoko_cabling: str = "qubit_flux_bias"
    attenuation_db: float = 0.0     # programmable attenuator, between the fridge_in switch and the fridge

    # ---- cartoon constants ----
    # 1.15e-15 W/Hz * 1 MHz = 1.15 nW: the zero-span level in twpa_calibration.ipynb
    chain_noise_w_per_hz: float = 1.15e-15
    hot_wire_w_per_hz_per_v2: float = 1.0e-14
    diode_w_per_hz: tuple[float, float] = (0.4e-15, 12e-15)   # (off, on) at the warm receiver
    fridge_atten_db: float = 103.55     # room temperature -> qubit ("A" in the notebook)
    chain_gain_db: float = 70.0         # qubit -> receiver
    # part of the chain noise that is already there at the TWPA input (amplified by its
    # gain); the rest is added after it (HEMT and up), so the pump improves the SNR
    twpa_input_noise_fraction: float = 0.1
    twpa_center_hz: float = 8.5e9       # pump frequency of the best gain
    twpa_bandwidth_hz: float = 1.0e9    # gain (in dB) falls off as a Gaussian of this width
    qubit_max_hz: float = 5.58e9        # flux sweet spot at 0 V
    flux_period_v: float = 2.0          # one flux quantum in bias volts
    qubit_linewidth_hz: float = 1e6
    qubit_depth: float = 0.6
    qubit_psat_w: float = 1e-17
    # SynthHD clock error against the SA (no shared 10 MHz): the rf_probe tone reaches the
    # fridge at frequency * (1 + this * 1e-6)
    synth_clock_error_ppm: float = 0.0
    unrouted_w_per_hz: float = 1e-21    # what a receiver sees when not switched in
    # extra tones at the SA input for tests: name -> (frequency Hz, power W)
    tones: dict[str, tuple[float, float]] = field(default_factory=dict)

    def __post_init__(self):
        self.rng = np.random.default_rng(self.seed)

    @classmethod
    def from_config(cls, sim_cfg: dict) -> "SimWorld":
        return cls(**{k: sim_cfg[k] for k in ("seed", "time_scale", "fail_rate") if k in sim_cfg})

    # ---- derived physics ----
    @property
    def noise_diode_v(self) -> float:
        return self.yoko_v if self.yoko_cabling == "noise_diode_bias" else 0.0

    @property
    def flux_bias_v(self) -> float:
        return self.yoko_v if self.yoko_cabling == "qubit_flux_bias" else 0.0

    def qubit_hz(self) -> float:
        return self.qubit_max_hz * np.sqrt(abs(np.cos(np.pi * self.flux_bias_v / self.flux_period_v)))

    def _source(self, name):
        s = self.sources.get(name)
        return s if s and s.get("on") else None

    def twpa_gain_db(self) -> float:
        pump = self._source("rf_pump")
        if pump is None or not 7e9 <= pump["frequency_hz"] <= 10e9:
            return 0.0
        shape = np.exp(-0.5 * ((pump["frequency_hz"] - self.twpa_center_hz) / self.twpa_bandwidth_hz) ** 2)
        return float(np.clip(3.0 * (pump["power_dbm"] + 3.0), 0.0, 20.0) * shape)

    def fridge_input(self):
        """(frequency, power W at room-temperature fridge input) or None."""
        name = "vna" if self.switch_ports["D"] == 1 else "rf_probe"
        src = self._source(name)
        if src is None:
            return None
        f = src["frequency_hz"] * (1 + self.synth_clock_error_ppm * 1e-6 if name == "rf_probe" else 1)
        return f, float(dbm_to_w(src["power_dbm"] - self.attenuation_db))

    def qubit_s21(self, freqs_hz, power_at_qubit_w: float) -> np.ndarray:
        depth = self.qubit_depth / (1 + power_at_qubit_w / self.qubit_psat_w)
        x = (np.asarray(freqs_hz) - self.qubit_hz()) / (self.qubit_linewidth_hz / 2)
        return 1 - depth / (1 + 1j * x)

    def _fridge_gain_db(self) -> float:
        return -self.fridge_atten_db + self.chain_gain_db + self.twpa_gain_db()

    def receiver_noise_w_per_hz(self, receiver_port: int) -> float:
        """Noise density at the input of the receiver on switch-B port 1 (vna) / 2 (spa)."""
        if self.switch_ports["B"] != receiver_port:
            return self.unrouted_w_per_hz
        if self.switch_ports["C"] == 2:
            return self.diode_w_per_hz[1] if self.noise_diode_v > 20 else self.diode_w_per_hz[0]
        g_twpa = 10 ** (self.twpa_gain_db() / 10)
        frac = self.twpa_input_noise_fraction
        return (self.chain_noise_w_per_hz * (frac * g_twpa + 1 - frac)
                + self.hot_wire_w_per_hz_per_v2 * self.noise_source_v ** 2 * g_twpa)

    def spa_power_w(self, freqs_hz: np.ndarray, rbw_hz: float) -> np.ndarray:
        """Mean power in an RBW filter centred at each frequency."""
        freqs_hz = np.asarray(freqs_hz, dtype=float)
        p = np.full(freqs_hz.shape, self.receiver_noise_w_per_hz(2) * rbw_hz)
        tones = list(self.tones.values())
        fin = self.fridge_input()
        if fin and self.switch_ports["B"] == 2 and self.switch_ports["C"] == 1:
            f0, p_in = fin
            p_qubit = p_in * 10 ** (-self.fridge_atten_db / 10)
            s = abs(self.qubit_s21(f0, p_qubit))
            tones.append((f0, p_in * s ** 2 * 10 ** (self._fridge_gain_db() / 10)))
        sigma = rbw_hz / 2.355
        for f0, p0 in tones:
            p += p0 * np.exp(-0.5 * ((freqs_hz - f0) / sigma) ** 2)
        return p

    def vna_s21(self, freqs_hz: np.ndarray, ifbw_hz: float) -> np.ndarray:
        """Complex S21 the VNA would measure, including receiver noise."""
        freqs_hz = np.asarray(freqs_hz, dtype=float)
        vna = self.sources.get("vna", {})
        routed = all(self.switch_ports[ch] == 1 for ch in "BCD")
        if routed and vna.get("on"):
            p_in = float(dbm_to_w(vna["power_dbm"] - self.attenuation_db))
            p_qubit = p_in * 10 ** (-self.fridge_atten_db / 10)
            g = 10 ** ((self._fridge_gain_db() - self.attenuation_db) / 20)
            s21 = g * self.qubit_s21(freqs_hz, p_qubit) * np.exp(-2j * np.pi * freqs_hz * 20e-9)
            signal_w = float(dbm_to_w(vna["power_dbm"]))    # S21 = b2/a1, a1 at the VNA port
        else:
            s21 = np.zeros(freqs_hz.shape, dtype=complex)
            signal_w = float(dbm_to_w(vna.get("power_dbm", -10)))
        noise_rel = np.sqrt(self.receiver_noise_w_per_hz(1) * ifbw_hz / signal_w / 2)
        noise = noise_rel * (self.rng.standard_normal(freqs_hz.shape) + 1j * self.rng.standard_normal(freqs_hz.shape))
        return s21 + noise
