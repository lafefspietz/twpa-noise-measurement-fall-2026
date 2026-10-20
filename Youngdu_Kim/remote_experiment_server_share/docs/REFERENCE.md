# Reference

Details behind the [README](../README.md): how the code is laid out, what each part of the web UI and each experiment does, what the saved JSON files contain, and the HTTP API.

## Instruments
| Name (`config.yaml`) | Instrument | Link | Driver |
|---|---|---|---|
| `spa` | HP E7405A spectrum analyzer | USB (VISA) | `e7405a.py` |
| `vna` | R&S ZVA24 | USB (RsInstrument) | `zva.py` |
| `rf_probe` | R&S SMW200A (probe / signal tone, RF A) | USB (VISA) | `smw200a.py` |
| `rf_synth` | Windfreak SynthHD (TWPA pump = B; A unused) | COM (windfreak package) | `windfreak.py` |
| `qubit_flux_bias` | Yokogawa GS210 | USB (VISA) | `gs210.py` |
| `noise_source_bias` | Agilent 33220A (DC, hot wire) | USB (VISA) | `fgen33220a.py` |
| `warm_switches` | Mini-Circuits RC-4SPDT-A18 | Ethernet HTTP | `rc4spdt.py` |
| `attenuator` | Mini-Circuits RCDAT-44G-63 | Ethernet HTTP | `rcdat.py` |

Every driver has the same interface: `idn()`, `get_state()`, `apply(**params)`, `safe_off()` and `close()`.
`apply()` checks the limits (`config.yaml`, changed in the settings tab) before sending anything, then reads the instrument's error queue. On the SA, an `apply()` whose only errors are the ones a command garbled on the USB-GPIB link gives (-1xx such as -102 "Syntax error", and -420 "Query UNTERMINATED") sends its settings again, up to 3 times in all, with a warning in the log; a value the SA refuses (-2xx) is not sent again.
The SA and the VNA also have `read_trace()`, and the SA has `measure_power()`.

## Layout
```
config.yaml              resource match strings, IPs, safety limits, init commands, sim settings
settings.local.yaml      changes made in the settings tab, applied over config.yaml (not in git)
labserver/
  transport.py           VisaTransport (pyvisa) / RsTransport (RsInstrument) / HttpTransport (Mini-Circuits)
  drivers/               one driver per instrument, with its simulator in the same file
  sim/
    scpi.py              SCPI header matching (short/long form, [optional], numeric suffix)
    transport.py         SimTransport: base class for fake SCPI instruments
    world.py             SimWorld: physical state shared by the simulators (cartoon physics)
  manager.py             InstrumentManager: connections, per-instrument locks, outputs off, shutdown
  server.py              FastAPI server (python -m labserver.server)
  live.py                live view: background trace reading per instrument, latest frame only
  experiments/           predefined experiments: settings (drive the UI form), plan check, run
                         (vna_sweep.py: shared part of the VNA 2D-map sweeps)
  jobs.py                runs one experiment at a time in a background thread, saves its record
  averaging.py           running average of live traces (complex for the VNA, power for the SA)
  storage.py             JSON records in the notebook layout (DD-MM-YYYY/<epoch>-<name>.json)
  settings.py            the settings tab: what can be changed, checks, settings.local.yaml
  report.py              interactive HTML page of a saved experiment record (python -m labserver.report)
  static/index.html      the web UI (no external libraries)
  static/results.js/.css result plots, shared by the experiments tab and the HTML export
run-server.sh / .bat     starts the server in the foreground (default: real instruments, LAN + Tailscale; --sim: simulators)
logs/                    server logs, one file per day
scripts/
  linux-setup.sh         one-time Linux setup (USB/serial access, no suspend, instrument LAN)
  restart-server.sh      starts or restarts the server in the background (Linux)
  post-receive.sh        optional git hook: restarts the server after a `git push` to its copy (setup in the file)
  bringup.py             on-site check of the instruments, one at a time
tests/                   pytest, runs against the simulators
```

### One driver, swappable transport
A driver (the SCPI logic) only talks to a `Transport`. For real instruments that is VISA, RsInstrument or HTTP; in sim mode it is a fake instrument.
The driver code tested against the simulators is the same code that runs on the hardware.

### SimWorld: instruments affect each other
The simulators write their outputs into `SimWorld` and read back what their inputs would see.
```
fridge_in (D): vna | rf_probe -> attenuator -> fridge (attenuation, qubit, TWPA, amplifiers) -> rx_source (C): fridge | noise_diode -> receiver (B): vna | spa
```
- The SA and VNA only see a signal when the switches route it to them.
- The hot wire (33220A) and the TWPA pump (Windfreak B) raise the SA noise floor. The TWPA gain peaks at a pump of 8.5 GHz and amplifies only the 10 % of the chain noise that is there at its input, so the pump also improves the probe's SNR. The GS210 flux bias moves the qubit frequency.
- A VNA CW tone shows up on the SA. Raising the VNA power saturates the qubit, so its dip gets shallower.
- The attenuator sits between the `fridge_in` switch and the fridge, so it attenuates both the VNA and the SMW200A probe.

The model is not calibrated. Its values are only plausible enough to run jobs and the UI end to end.
Only the base noise level is matched to the notebook's zero-span value (≈1.15 nW at RBW 1 MHz).

## Web UI
Open `http://<server>:8000/`. It is a single page, black on white, with no external libraries (works offline), with three tabs: **instruments** (below), **experiments** (see [Experiments](#experiments)) and **settings** (see [Settings tab](#settings-tab)).
- A status line (connections, a running experiment), plus **outputs off** and **shut down server**. **outputs off** turns off the pump, signal, flux bias, VNA RF and hot wire, and the server keeps running; it asks first only when an experiment is running, because that experiment stops. **shut down server** asks for confirmation, then stops the server process and leaves the outputs as they are; afterwards the server has to be started again on the instrument computer.
- **Live plot**: SA or VNA (|S21| dB or phase), hold, **avg N** (running average of the last N traces, done on the server: complex mean for the VNA, mean power for the SA; it restarts by itself when a setting changes, **restart** starts it over), auto or fixed y range, hover readout, and "save trace" (label + note): writes the JSON under `data/` on the server and puts a copy on the computer running the browser. With a folder chosen ("choose folder", remembered by the browser) the copy goes to `<folder>/<label>/<file>` (`a/b` in the label makes nested folders); otherwise it is a normal download.
- **Settings for the selected instrument** (SA: center/span/RBW/VBW/ref level/sweep time/scale (LOG/LIN)/unit/detector/avg type; VNA: start/stop/points/IFBW/power/sweep/CW/RF output). Enter in any field (or "apply") sends only the fields that changed. The page then shows the **full state** the instrument reports back, because one setting can change others (span → auto RBW/VBW → sweep time; values rounded by the instrument, e.g. RBW 1.5 → 1 kHz). Only failures show a message.
  SA detectors: sample, peak, neg peak, normal (our E7405A reads NORM back as POS), average (`RMS` reads back as AVER). **avg type** is what the average detector averages: power (RMS, in W) or log (video, in dB; the E7405A reads LOG back as VID, and Gaussian noise then reads ~2.5 dB low); the VBW filter ignores it and averages in the vertical scale (dB on LOG, voltage on LIN). With the average detector set a long sweep time, about 1 s: each point averages only ~125 independent samples per second of sweep at RBW 100 kHz, and none at 10 ms or less (see the [Y factor detector](#y-factor-measurement-y_factor_measurement) notes). An empty sweep time is not sent.
- **SA alignment** (SA view): the alignment status the SA reports (`STAT:QUES:CAL:COND?`: ALIGN NEEDED, failures, RF alignment pending), read with the settings. The server keeps the SA's auto alignment off (`:CAL:AUTO OFF`, it caused random VISA timeouts), so the SA drifts with time and temperature: on 2026-10-14 its RBW filters had drifted so far that one tone read −44..−72 dBm depending on the RBW, until an alignment fixed it. Align at the start of a session and when the line shows ALIGN NEEDED: **do align** sends Align Now, All (`:CAL:ALL`, the same as System → Alignments → Align Now → All on the front panel). It takes about 3–6 min and can't be stopped once started. Meanwhile the SA is reserved, so the live view pauses (a trace read during an alignment hangs the SA's GPIB) and experiments can't use it; the line counts the seconds, and the result shows when it ends. The RF part needs a cable from AMPTD REF OUT to INPUT; without it the SA skips RF (+652, not reported as an error), aligns only the filters (IF) and the rest, and the line shows "RF alignment pending". Reference: E7400A Series Programmer's Guide (E7401-90053), STATus:QUEStionable:CALibration (p. 83–85).
- **Knobs**: hover a knob and turn the mouse wheel. Each tick moves the value by that knob's step. Every value also has a number box (type, then Enter).
  - Pump: on, f (100/10/1 MHz, 100 kHz), P (1/0.1/0.01 dB)
  - Signal (SMW200A): on, f (100/10/1 MHz, 100 kHz), P (1/0.1/0.01 dB). The panel flags in bold anything that makes the tone differ from what the knobs say: modulation or IQ on, a frequency/level mode other than CW, a frequency or level offset (all set on the SMW's front panel; the server never changes them).
  - Qubit flux: on, V (0.1 V/10 mV/1 mV/0.1 mV). If the GS210 is in current mode (CURR), the panel says so in bold.
  - Noise source (33220A hot wire): on (also selects DC mode), V (0.1 V/10 mV/1 mV)
  - Attenuator: A (10/1/0.25 dB)

  The panels sit in two columns (one on narrow windows): Pump | Signal, Qubit flux | Noise source, Attenuator | Switches.
  **Switches** has one row per named switch in `config.yaml` (fridge_in, rx_source, receiver). Click a position to move it; the row then shows what the switch box reports.

  Fast wheel turns are coalesced: only the latest value is sent while a request is in flight. Values are clamped to the instrument limits (an open end clamps nothing).
- **The page never polls the instruments.** It reads their values on page load, on **refresh** (next to apply, with the time of the last read), and from the snapshot that "save trace" records anyway. Every change also shows the state the instrument returns. Front-panel changes show up after refresh or save. Only the status line is polled every 2 s; it reads server-side flags and talks to no instrument. (The SA/VNA live view does read its instrument, but only while someone is watching it.)

## Experiments
The **experiments** tab lists predefined sweeps. Click one to open its settings.
- The form is built from the experiment's settings on the server (`labserver/experiments/`), so adding an experiment needs no UI code.
- Every change is checked on the server (`plan`): number of points and traces, a rough duration and data size, errors (instrument limits and experiment caps from the settings tab), warnings, and the table of sweep points. Checking changes no instrument; it only reads them. A setting left empty that the server works out from the others (e.g. the Y factor sweep time) shows that value in gray.
- **load current values** fills the fields marked `*` from the instruments. **defaults** resets the form. The browser remembers the last settings for each experiment.
- **save as defaults** saves the form on the server (`settings.local.yaml`): it is what the form starts with in any browser that has not used the experiment yet, and what **defaults** goes back to. Only values that differ from the built-in defaults are kept; the settings tab puts them back to built-in.
- **start** (after a confirmation) runs it on the server as a job ([labserver/jobs.py](../labserver/jobs.py)), one at a time. The job reserves its instruments, so the UI and the live view can't change them until it ends. The page shows progress and time left, and **stop** ends it after the trace being read. However it ends (done, stopped, outputs off, error) the points measured so far are saved under `data/` with `"status"`, and a download link appears.
- **download interactive HTML** (next to the JSON link) gives one HTML file of the run that opens in any browser, offline: the same plots as the tab (results.js inlined, hover readouts, pinning; the TWPA pump and signal sweeps with each point's spectrum when "save the averaged spectrum of every point" was on), a table under the plot with every per-point value of the record for the point under the cursor (times, traces, W values with dBm, ...), then the run (status, times, notes, "when done"), the settings as the form showed them (hover for the help), the state of every instrument at the start (the ones the experiment uses first), the states read back during the run (`spa_setup`, `synth_state`, ...), and every other value in the record. The page is built from the saved JSON on request (`/api/data/.../<file>.html`), so it works for any experiment record on the server; `python -m labserver.report <record.json> ...` writes `<record>.html` next to JSON files copied elsewhere. The JSON stays the data file: the page only lists the big arrays it holds.
- The live view stops while this tab is open, so the SA/VNA are not read for nothing.
- The plan also checks what the run will send against the instrument limits (VNA start/stop/points/IFBW/power, SA frequency/span/RBW/VBW/sweep time/reference level, the swept bias or attenuation), so a run is refused before it starts instead of failing at the first point outside a limit.

### Qubit flux sweep (`qubit_flux_sweep`)
The first measurement: qubit frequency vs flux. Steps the **GS210 flux bias voltage** and records averaged S21 around the qubit at each step, with the VNA band, IFBW, power and traces per point fixed (like `qubit-flux-sweep.ipynb`: −0.28..−0.24 V in 201 points, one trace each, which is the default). The dip follows the qubit, so the map shows f_q(V); pick the bias for the power sweep from it.
- **Flux bias**: from / to / step (V), traces per point, wait after each step. Limited by the qubit_flux_bias level (VOLT mode) limit (now −1..1 V), at most 2000 points (both in the settings tab).
- **VNA**: start/stop/points/IFBW/power, fixed for the whole sweep. Keep the power below saturation (see the power sweep).
- Before: warm switches to VNA → fridge → VNA, then the flux bias. If its output is off it is turned on at 0 V and ramped to the first point; if on, it is ramped from where it is (the driver ramps changes larger than `ramp_step`). The source must be in VOLT mode. If its range is too small for the sweep, the sweep sets it, but only while the output is off; with the output on it is an error in the plan check.
- After: restore the VNA, switches and flux bias as they were (ramped back; if it was off, ramped to 0 V and turned off), leave everything at the last point, or turn the VNA RF off and the flux bias to 0 V and off. After "outputs off" nothing is sent.
- **Result**: the same 2D map as the power sweep with y = flux bias [V]; the cuts are S21 vs frequency at one bias and S21 vs bias at one frequency.
- Saved JSON: `flux_bias` (V) in place of `power_dbm`, otherwise as the power sweep below.

### Qubit power sweep (`qubit_power_sweep`)
Finds where the qubit power-calibration sensor saturates: steps the drive power and records averaged S21 around the qubit at each step (like `vna-power-sweep.ipynb`). **Step the power with** picks how:
- **VNA output power** (default): the VNA power is stepped; the attenuator is not changed, its value is in the saved snapshot.
- **Warm step attenuator (VNA power fixed)**: the VNA stays at one power and the RCDAT is stepped instead, so the VNA's own AUTO step attenuator never switches during the sweep (it can shift a flux-sensitive qubit by a few MHz at each switch). **Attenuation segments** are rows of from / to / step / traces per point in dB, high attenuation (low power) first, multiples of 0.25 dB, within the attenuator limit. Default: VNA −10 dBm, 63..55 dB step 2 × 30 traces, 55..45 × 10, 45..35 × 3, 35..15 × 1, the same drive as VNA powers −48..0 dBm at 25 dB. The attenuator is set to the first point before the RF is turned on.

The attenuator is reserved in both modes, so nobody changes it during a sweep.
- **Power segments**: rows of from / to / step / traces per point, swept top to bottom. Low powers are noisier, so they can get more averaging (default: −80..−60 dBm step 2 × 100 traces, −60..−30 step 1 × 30, −30..0 step 1 × 10). A point shared by two segments is measured once; overlapping segments are refused.
- **Averaging**: the mean of single-sweep complex traces (as in the notebook). Optionally every trace is saved too.
- VNA power range: limited by the vna power limit (now −80..0 dBm). Our ZVA24 (`*OPT?`: B16, B21, B22, B31, B32) has a 0–70 dB generator step attenuator on port 1, in AUTO mode. Data sheet range at 5–6 GHz: −100 to +11 dBm (base −30..+13, lower limit −70 dB with B21, upper limit −1 dB each for B21 and B16). Below about −30 dBm the AUTO step attenuator (10 dB steps) switches as the power crosses a boundary; linearity with ALC off is < 2 dB above −70 dBm and < 3 dB from −70 to −100 dBm.
- At each point: VNA start/stop/points/IFBW and a wait after each power step.
- Before: set the warm switches to VNA → fridge → VNA, (attenuator mode: set the attenuator to the first point), set the VNA, RF on. After: restore the VNA, switches and attenuator as they were, leave the last point, or turn the VNA RF off (the attenuator stays at the last point). The attenuator is restored around the VNA so the drive never goes above both the sweep's and the old one: raised to the old value first if that is higher, lowered after the VNA is restored. After "outputs off" nothing is sent.
- **Result**: a 2D map, x = frequency, y = VNA power (or attenuation), gray scale (|S21| dB or phase), filled in as the sweep runs. In attenuator mode the map and cuts show |S21| + attenuation, because the attenuator is in the measured path and scales S21 itself. Hover shows two cuts: S21 vs frequency at that power, and S21 vs power at that frequency (the saturation curve). Click pins the cursor. Without a cursor the cuts show the newest power and the dip of the lowest power.
- Saved JSON: `frequency_hz`, `fghz`, `power_dbm`, `averages`, `s_real`/`s_imag` [power][frequency] (the means), `point_start_time`/`point_end_time` (epoch s when each point's traces started and ended, to tell drifts in time from the swept setting), optionally `traces_real`/`traces_imag`, `params`, `instruments` (snapshot before the sweep), `status`, `start_time`, `finish_time`, `sweep_time`.
- Attenuator mode saves `attenuation_db` in place of `power_dbm`, plus `drive_dbm` = VNA power − attenuation (the power after the attenuator); the VNA power is `params.vna_power_dbm`. `s_real`/`s_imag` are as measured, so they include the attenuator: multiply by 10^(attenuation_db/20) before normalizing by another trace. The RCDAT's own step error (a few tenths of a dB and some phase per state) stays in the data; normalize each trace off resonance, or measure the through path vs attenuation with the qubit detuned.

### Qubit power sweep on the SA (`qubit_power_sweep_sa`)
The qubit power sweep with the **SMW200A probe** as the source and the **SA** as the receiver instead of the VNA: at each drive power the probe frequency is stepped across the qubit, and the SA, **centred on the probe frequency** (plus an optional fixed offset for unlocked clocks) with a narrow span, takes an averaged spectrum at every frequency. From it: the **tone** = max of the bins within ± *tone window* of the SA center (`peak_w`; `signal_w` = peak − noise, `null` where the tone is not above the noise), and the **noise** = mean (in W) of the bins *noise from* … *noise to* away from it, on both sides. Power is the outer loop (low drive first), frequency the inner one. **Absolute power only**: there is no reference measurement (qubit detuned or off resonance); normalise in the analysis.
- **Step the power with**: **SMW200A probe power** (default; segments of from / to / step / traces per point, as the VNA power segments; rf_probe probe power limit, now −145..0 dBm; the warm attenuator is left as it is or set once) or **warm step attenuator** (probe power fixed, attenuation segments high attenuation first, multiples of 0.25 dB; default probe −10 dBm, 63..15 dB). Either way the attenuator is reserved, and `drive_dbm` = probe power − attenuation (the power after it, at `fridge_in`).
- **Line attenuation fridge_in → qubit** (optional): if given, the record also has `qubit_power_dbm` = `drive_dbm` − it. It does not count the warm attenuator and is for the SMW200A path; the VNA fit (102.4 dB from the VNA port with the attenuator at 25 dB) is not the same number.
- **Probe frequency**: from / to / step (default 5.555–5.605 GHz in 1 MHz steps; from = to gives one frequency), the wait after each frequency and power step (also after the probe is turned on). At most 2000 frequencies, and powers × frequencies within the total point cap (settings tab).
- **SA center − probe frequency** (kHz, default 0): the SA center at every point is the probe frequency plus this fixed offset. Without a shared 10 MHz reference the tone shows up a little off the probe frequency on the SA: set the offset to where it shows up (e.g. from the live SA view with the probe on), and the tone window and the noise region follow it. The clock error is in ppm, so the offset drifts a little across the sweep (2 ppm over 50 MHz: 100 Hz), which the tone window covers. The plan check warns above 20 ppm of the probe frequency (a wrong sign or unit, most likely); `peak_hz` is the tone frequency as the SA reads it.
- **SA**: span (default 100 kHz, 401 bins), RBW, VBW (default 1 kHz each), sweep time (empty = auto), reference level. Sample detector, LOG, dBm; the traces are averaged in W, as in the TWPA pump sweep. The center follows the probe (plus the offset) at every point (one SMW200A and one SA command per frequency step, so a point costs a little more than its sweeps). Saving every averaged spectrum (401 numbers per point) is off by default.
- **Tone and noise**: tone window (default ± 15 kHz around the SA center, for what the offset leaves of the clock error), noise from (default 30 kHz from the SA center) / to (empty = edge of the span). The plan check refuses a window that reaches into the noise region and a noise region with fewer than 10 bins on a side, and warns when the noise starts within 3 RBW of the window, the window is narrower than a bin, or a bin is wider than the RBW.
- **Qubit flux bias**: leave it, or set it for the sweep (as in the TWPA pump sweep). The TWPA pump is left as it is (its state is in the snapshot).
- Before: warm switches `fridge_in` → rf_probe, `rx_source` → fridge, `receiver` → spa, then the lowest drive (attenuator mode: the attenuator at the first point; otherwise the attenuator if set, and the probe at the first power), the flux bias (if set), the SA, then the probe on. After: restore the SA, switches, attenuator, probe and flux bias as they were (the attenuator raised first if it was higher, lowered after the probe is restored, so the drive never exceeds both the sweep's and the old one) / leave everything at the last point / turn the probe off (and the flux bias, if the sweep set it). After "outputs off" nothing is sent.
- **Result**: the same 2D map as the VNA power sweep, x = probe frequency, y = probe power (or attenuation), of **tone − probe power** in dB (like the VNA S21 it includes the attenuator; in attenuator mode the map adds it back, so it shows tone − drive). One row per power, added when its frequency line is done. Cuts: vs frequency at one power, and vs power at one frequency (the saturation curve). No phase.
- **SA log bias**: as in the TWPA pump sweep, the noise reads about 2.5 dB low with VBW ≪ RBW on LOG; the tone does not (`analysis_note`).
- Saved JSON: `frequency_hz` (the probe frequencies), `probe_power_dbm` (or `attenuation_db`), `drive_dbm`, optionally `qubit_power_dbm` and `line_atten_db`, `averages`, `point_start_time`/`point_end_time` (per power line), `peak_w`, `peak_hz`, `signal_w`, `noise_w`, `noise_std_w` [power][frequency], optionally `traces_w` [power][frequency][401], `sa_center_offset_hz` (SA center − probe frequency), `sa_bin_offset_hz` (the 401 bin offsets from the SA center), `noise_inner_hz`/`noise_outer_hz`/`signal_window_hz`, `rbw_hz`/`vbw_hz`/`sweep_time_s` and `spa_setup` (as the SA took them), `probe_state` (the SMW200A once the probe was on), `attenuator_state`, `flux_bias_state`, `analysis_note`, `fridge_temperature_k`, `params`, `instruments`, `status`, `start_time`, `finish_time`, `sweep_time`.

### Y factor measurement (`y_factor_measurement`)
Takes the data for a Y-factor analysis of the receive chain (the fit for T_N and gain is done separately): steps the **noise source bias** (33220A DC offset, the hot wire) and records the **zero-span noise power on the SA** at one frequency at each step, like `noise-sweep-zero-power.ipynb` (−0.2..0.2 V in 41 points, one trace each, which is the default).
- **Noise source bias**: from / to / step (V), traces per point (401-sample zero-span traces), wait after each step (for the hot wire to settle), passes. Limited by the noise_source_bias DC offset limit (now −0.2..0.2 V). Passes > 1 repeat the sweep, every other pass in reverse, so drifts show up as hysteresis.
- **SA**: frequency, RBW (default 100 kHz), VBW (default 3 MHz), sweep time (empty = 1 s, shown in gray; a typed value is used as is. Zero span has no auto sweep time, the E7405A answers +620 "Can't Auto-Couple Sweep Time in Zero Span", so a value is always sent). The SA takes RBW/VBW in 1-3-10 steps: the run reads back what it took, notes any difference, and shows the values above the plot, reference level in dBm or W (W: e.g. 2e-8; **load current values** fills it from the SA, which must then be in W), **vertical scale** LOG (default) or LIN. The scale is separate from the unit: in LIN our E7405A only reads right from the reference level down to about 20 dB below it (above it clips, below it the trace sits on a floor near ref − 20 dB, seen 2026-10-14), so the plan check warns about LIN. The SA is set to zero span, the **average detector with power (RMS) averaging** (`:DET AVER`, `:AVER:TYPE POW`), the chosen scale, unit W (as the notebooks), and the reference level is sent in W; the run stops if the SA reads back another detector or average type. The input attenuation stays as `init_commands` set it, and the average type stays POW after the run. If the SA's own averaging is on, the plan check warns (each trace is then that many sweeps).
- **Why the average detector, always** (checked on our E7405A on 2026-10-20 against the noise power: the mean in W of sample-detector samples at VBW ≥ 30 × RBW). Each of the 401 points is the mean in W of the samples in it, so it reads the noise power itself, whatever the signal statistics (a tone in the noise too): 0.00 dB at RBW 100 kHz, −0.03 dB at 10 kHz, with VBW 3 MHz, on LOG or LIN. The sample detector with VBW ≪ RBW, used before, reads Gaussian noise low instead, because the VBW filter averages in the vertical scale whatever the `:AVER:TYPE`: in dB on LOG (2.5 dB low at RBW 1 MHz, 2.15 dB at 100 kHz, 1.77 dB at 10 kHz, the same from VBW = RBW/100 to RBW/10000), in voltage on LIN (1.0 dB at 100 kHz, 1.2 dB at 10 kHz). The record keeps what the SA read; any correction is left to the analysis. The plan check warns about settings that make the average detector read low:
  - RBW above 100 kHz: at 1 MHz it reads ~1 dB low (VBW 3 MHz, the maximum, is not ≫ RBW, and the VBW filter averages in the vertical scale before the detector).
  - VBW below 30 × RBW, for the same reason.
  - **A sweep shorter than 0.5 s. Use about 1 s**: each point averages only ~125 independent samples per second of sweep at RBW 100 kHz (~40 at 10 kHz, ~170 at 1 MHz), and at 10 ms or less it does not average at all. RBW 100 kHz averages fastest.
- **Qubit flux bias**: leave it as it is, or set it to a fixed voltage for the sweep (to move the qubit away from the measurement frequency, or to park it there on purpose). It is reserved either way and its state during the sweep is saved. Set mode works like the flux sweep: within the qubit_flux_bias level limit, VOLT mode, turned on at 0 V and ramped if it is off.
- **TWPA pump** (SynthHD channel B): leave it as it is, set a frequency and power and turn it on for the sweep (TWPA on), or turn it off for the sweep (TWPA off, the reference). Set mode is limited by the rf_synth pump frequency/power limits (now −80..10 dBm) and the SynthHD range (0.01–15 GHz, −80..20 dBm); the power is set before the output goes on. Then it waits (default 1 s) so the TWPA settles. Its state during the sweep is saved in every mode, and the run notes it if the pump channel is not locked.
- The plan check notes it if the SMW200A probe is on (the sweep turns it off) and what happens to the TWPA pump. If the switches are left as they are with `fridge_in` on the VNA, it warns when the VNA RF is on.
- Before: SMW200A probe off, the TWPA pump (set/off mode), warm switches `fridge_in` → rf_probe, `rx_source` → fridge, `receiver` → spa (so nothing drives the fridge input; the attenuator between `fridge_in` and the fridge is not changed), the flux bias (set mode), the SA, then the noise source: if its output is off it is set to DC at the first point and turned on; if it is on it must already be in DC mode (otherwise the plan check gives an error).
- After: restore the SA, switches, probe, TWPA pump, flux bias and noise source as they were (RBW/VBW auto coupling is not restored, only the values; the probe goes back on after the switches) / leave everything at the last point (the probe stays off) / turn the noise source off at 0 V, and the TWPA pump and the flux bias (ramped to 0 V) if the sweep set them. After "outputs off" nothing is sent.
- **Result**: noise power vs bias (nW or dBm), in the order measured, filled in as it runs. Hover reads the nearest point (of two passes at one bias, the nearer one). If any sample is above the reference level, the run notes that the SA may have clipped.
- Saved JSON: `bias_v`, `pass`, `traces`, `power_w` (mean of every sample at that point), `power_std_w` (std of those samples), `trace_mean_w`/`trace_std_w` [point][trace], optionally `samples_w` [point][trace][401], `frequency_hz`, `rbw_hz`/`vbw_hz`/`sweep_time_s`/`detector`/`average_type` (as the SA took them; the typed values are in `params`), `spa_setup` (the whole SA state the sweep used), `flux_bias_state` (the flux bias during the sweep), `pump_state` (`pump_enable`, `pump_frequency_hz`, `pump_power_dbm`, `pump_locked` during the sweep), `fridge_temperature_k` (typed in), `point_start_time`/`point_end_time`, `params` (only the ones the form shows for the choices made: `ref_level_dbm` or `ref_level_w`, no pump or flux values when those are left as they are), `instruments`, `status`, `start_time`, `finish_time`, `sweep_time`.

### TWPA pump sweep (`twpa_pump_sweep`)
Tunes up the TWPA pump: steps the **pump** (SynthHD channel B) over a **pump frequency × pump power grid** while the **SMW200A probe** sends one tone through the fridge, and takes an **SA spectrum** (not zero span) around the SA frequency at each point. A **pump-off reference** spectrum is taken once at the start, and every point is compared with it:
- **noise** = mean (in W) of the bins with *noise from* ≤ |f − f_SA| ≤ *noise to*, on both sides of the SA frequency (*noise to* empty = to the edge of the span)
- **signal** = max of the bins within ± *signal window* of the probe frequency or the SA frequency (*signal window around*; empty window = the RBW the SA takes), minus the noise; the raw max is saved as `peak_w`
- **gain** = signal / signal_ref, **noise rise** = noise / noise_ref, **SNRI** = (signal / noise) / (signal_ref / noise_ref) = gain − noise rise (in dB)

Settings:
- **Pump sweep**: pump frequency from / to / step (GHz, the outer loop), pump power from / to / step (dBm, the inner loop). From = to gives one value, so a power sweep or a frequency sweep is the same experiment. Also traces per point (spectra averaged in W, also for the reference), the wait after each pump step, and the wait after the pump goes off (before the reference) and after it is first turned on. Limited by the rf_synth pump frequency/power limits (now −80..10 dBm) and the SynthHD range.
- **Probe**: frequency and power (rf_probe probe limits), and the warm step attenuator (leave it, or set it for the sweep; multiples of 0.25 dB).
- **SA**: SA frequency (the center), span, RBW, VBW, sweep time (empty = the SA's auto sweep time; the plan's estimate is rough, the run saves what the SA took), reference level (dBm). The run sets the sample detector, LOG scale and unit dBm, and averages the traces in W. The SA takes RBW/VBW in 1-3-10 steps: the run reads back what it took and notes any difference.
- **Signal and noise**: noise from / to (MHz from the SA frequency), *signal window around* the probe frequency (default) or the SA frequency, and the signal window (± kHz). Without a shared 10 MHz reference the SMW200A and the SA disagree a little, so the tone shows up a little off the probe frequency as the SA reads it: centre the window where the tone actually is (e.g. on the SA frequency when the probe is set to it) and make it wide enough for the offset. The plan checks that both the window and the probe tone stay out of the noise region, and with the window around the SA it warns when the probe is set outside it.
- **Qubit flux bias**: leave it, or set it for the sweep (as in the Y factor measurement), to keep the qubit away from the probe.

The plan check refuses a noise region that is empty or beyond the span, has fewer than 10 bins on a side, or overlaps the probe tone ± window, and a probe outside the span. It warns when the noise region starts within 3 RBW of the tone, when the window is narrower than the bin spacing (span / 400; the nearest bin is used), and when the bin spacing is wider than the RBW (the bins may miss the top of the tone).

- Before: warm switches `fridge_in` → rf_probe, `rx_source` → fridge, `receiver` → spa (as the Y factor measurement), the attenuator and flux bias (if set), the SA, then the probe on, pump off, wait, reference. Then the pump frequency and power are set before its output goes on, it waits again, and the grid runs (only the setting that changed is sent at each step).
- After: restore the SA, switches, attenuator, probe, pump and flux bias as they were (RBW/VBW auto coupling is not restored, only the values) / leave everything at the last point / turn the probe and the pump off (and the flux bias, ramped to 0 V, if the sweep set it). After "outputs off" nothing is sent.
- **Result**: a gray map (dark = low) of gain, SNRI, noise rise, SNR, signal or noise over pump frequency × pump power, filled in as it runs. With one value on an axis it is a line plot vs the other axis. Below it, the newest point's spectrum over the pump-off reference, with the noise region (blue) and the signal window (red). Hover a point for its values and spectrum; click it to pin it, click it again to follow the newest. Hover the spectrum to read a bin. Points where the tone is not above the noise have no gain/SNRI (red cells).
- **SA log bias**: with VBW ≪ RBW on a LOG scale the SA averages the log of the noise, so the noise reads about 2.5 dB low while the tone does not. It cancels in the noise rise and SNRI (the same setup with the pump on and off), but not in the absolute SNR or in the peak − noise subtraction (small when the SNR is high). The record says so in `analysis_note`.
- Saved JSON: `pump_frequency_hz`, `pump_power_dbm`, `traces`, `signal_w`, `peak_w`, `peak_hz`, `noise_w`, `noise_std_w`, `gain_db`, `noise_rise_db`, `snri_db`, `snr_db` (per point; `null` where undefined), `point_start_time`/`point_end_time`, `reference` (`signal_w`, `peak_w`, `peak_hz`, `noise_w`, `noise_std_w`, `n_noise`, `snr_db`, `trace_w`, `start_time`, `end_time`), optionally `traces_w` [point][401] (the averaged spectra in W), `frequency_hz` (the 401 bin frequencies), `probe_frequency_hz`, `sa_frequency_hz`, `noise_inner_hz`/`noise_outer_hz`/`signal_window_hz`/`signal_center_hz` (as used; `signal_center_hz` is the window's center), `rbw_hz`/`vbw_hz`/`sweep_time_s` and `spa_setup` (as the SA took them), `synth_state` (the SynthHD right after the pump went on), `probe_state` (the SMW200A once the probe was on), `attenuator_state`, `flux_bias_state`, `analysis_note`, `fridge_temperature_k`, `params`, `instruments`, `status`, `start_time`, `finish_time`, `sweep_time`.

### TWPA signal sweep (`twpa_signal_sweep`)
Measures the SNR at a **fixed TWPA pump** while the **signal** (the SMW200A probe) is stepped, and optionally the **noise source bias** (33220A DC offset, the hot wire, whose noise reaches the TWPA input and goes as V²) as well, to see how the SNR falls as the input noise rises. At each point the **SA**, centred on the signal frequency (plus an optional fixed offset) with a narrow span, takes an averaged spectrum. The signal and noise are read as in the TWPA pump sweep, with the signal window around the SA center: **SNR** = signal / noise. With the pump-off reference on, every point is also measured with the pump off, and **gain**, **noise rise** and **SNRI** (= SNR − SNR_pump-off, in dB) are against that point's own pump-off spectrum (the same signal and bias).

Order: the noise source bias is the outer loop. At each bias: the pump off and every signal point (the pump-off pass), then the pump on and the same points again. The pump goes off and on once per bias; without the reference it is turned on once and stays on.

Settings:
- **Signal**: *step the signal* by **frequency** (from / to / step, power fixed; the default, 5.5–6.5 GHz in 100 MHz steps at −20 dBm) or by **power** (from / to / step at one frequency). From = to gives one value. Also traces per point (spectra averaged in W, pump on and off), the wait after each signal step (also after the signal is first turned on), and the warm step attenuator (leave it, or set it for the sweep; multiples of 0.25 dB). Limited by the rf_probe probe frequency/power limits (now −145..0 dBm) and the SMW200A range (0.0001–44 GHz, −145..30 dBm).
- **TWPA pump (fixed)**: frequency and power (default 8.5 GHz, 7 dBm; rf_synth pump limits, now −80..10 dBm), set before its output goes on; *pump-off reference*: a pump-off pass at each bias (default) or none (pump on only, SNR only); the wait after the pump goes off / on.
- **Noise source bias**: leave it as it is (default; the bias it applies, its DC offset or 0 V with the output off, is recorded at every point) or **step it** from / to / step (V, the outer loop; noise_source_bias DC offset limit, now −0.2..0.2 V), with a wait after each bias step. Stepping works as in the Y factor measurement: an output that is off is set to DC at the first bias and turned on; one that is on must already be in DC (otherwise the plan check gives an error). Either way it is reserved during the run, and its state at the first bias is saved.
- **SA**: *SA center − signal frequency* (kHz, default 0; as in the qubit power sweep on the SA, for clocks without a shared 10 MHz reference), span (default 5 MHz), RBW (30 kHz), VBW (1 kHz), sweep time (empty = auto), reference level. Sample detector, LOG, dBm. The center follows the signal at every frequency step. Saving the averaged spectra (pump on and off, 2 × 401 numbers per point) is on by default.
- **Signal and noise**: signal window (± kHz around the SA center; empty = the RBW the SA takes), noise from / to (MHz from the SA center; to empty = edge of the span).
- **Qubit flux bias**: leave it, or set it for the sweep, to keep the qubit away from the signal frequencies.

The plan check refuses a noise region that is empty, beyond the span or with fewer than 10 bins on a side, and a signal window that reaches into it. It warns when the noise starts within 3 RBW of the window, when the window is narrower than a bin or a bin wider than the RBW, when the SA center offset is above 20 ppm of the signal frequency, and when the pump falls inside the SA span at a signal frequency (it and its idler would land in the spectrum). With the noise source left as it is, it warns if its output is on in a mode other than DC.

- Before: warm switches `fridge_in` → rf_probe, `rx_source` → fridge, `receiver` → spa, the attenuator and flux bias (if set), the noise source at the first bias (if stepped), the SA, then the pump (off for a pump-off pass; its frequency and power set either way), the signal on at the first point, and the waits.
- After: restore the SA, switches, attenuator, signal, pump, noise source (if stepped) and flux bias as they were / leave everything at the last point / turn the signal and the pump off (and the noise source at 0 V and off, and the flux bias ramped to 0 V, if the sweep set them). After "outputs off" nothing is sent.
- **Result**: the TWPA pump sweep's map, x = signal frequency (or power), y = noise source bias, of SNR (the default), gain, SNRI, noise rise, signal or noise (only SNR, signal and noise without the reference). With one bias it is a line plot vs the signal. Below it, the spectrum of the point under the cursor over its own pump-off spectrum (both only if saved), on that point's SA center.
- **SA log bias**: as in the TWPA pump sweep, the noise reads about 2.5 dB low with VBW ≪ RBW on LOG while the tone does not; it cancels in the noise rise and SNRI, not in the absolute SNR (`analysis_note`).
- Saved JSON, one entry per point in the order measured (bias outer): `signal_frequency_hz`, `signal_power_dbm`, `noise_bias_v`, `traces`, `sa_center_hz`, `point_start_time`/`point_end_time` (the pump-on spectrum), `signal_w`, `peak_w`, `peak_hz`, `noise_w`, `noise_std_w`, `snr_db` (pump on; `null` where undefined); with the reference also `ref_signal_w`, `ref_peak_w`, `ref_peak_hz`, `ref_noise_w`, `ref_noise_std_w`, `ref_snr_db`, `ref_start_time`/`ref_end_time` (the pump-off spectrum of the same point), `gain_db`, `noise_rise_db`, `snri_db`; optionally `traces_w` and `ref_traces_w` [point][401]. Also `pump_frequency_hz`, `pump_power_dbm`, `sa_center_offset_hz`, `sa_bin_offset_hz` (the 401 bin offsets from the SA center), `noise_inner_hz`/`noise_outer_hz`/`signal_window_hz`, `rbw_hz`/`vbw_hz`/`sweep_time_s` and `spa_setup` (as the SA took them), `synth_state` (the SynthHD once the pump was first on), `probe_state` (the SMW200A once the signal was on), `noise_source_state`, `attenuator_state`, `flux_bias_state`, `analysis_note`, `fridge_temperature_k`, `params`, `instruments`, `status`, `start_time`, `finish_time`, `sweep_time`. A stopped run keeps only the points measured with the pump on (and their pump-off values).

### TWPA noise spectrum (`twpa_noise_spectrum`)
Takes the data for the TWPA gain and the system noise vs frequency: swept **SA spectra** (401 points over a wide span, e.g. 1 GHz) in **four states**, the hot wire at 0 V and at a bias × the TWPA pump off and on, repeated in rounds. The analysis then gets T_add and T_sys at every frequency from the hot-wire Y and the hot-wire noise temperatures of the calibration; the run keeps only what needs no calibration.
- **Rounds**: rounds, spectra per state per round, wait after each hot-wire step and after the pump goes on / off. Each round takes off-0, off-on, on-on, on-0 (pump, hot wire), every other round in reverse, so the pump toggles once per round and slow drifts cancel between the states. Each state's spectra are averaged in W over all rounds.
- **SA**: center, span, RBW (default 100 kHz), VBW (default 3 MHz), sweep time (default 10 s), reference level (default −60 dBm). Always the average detector with power averaging (`:DET AVER`, `:AVER:TYPE POW`), LOG, unit dBm; the run stops if the SA reads back another detector or average type. The plan check warns about an RBW above 100 kHz and a VBW below 30 × RBW (as in the Y factor measurement), refuses a span that contains the pump, and warns when the pump is within 50 MHz of it. The notes give the averaging per point: ~125 independent samples per second of sweep at RBW 100 kHz. If a state's spectrum comes within 10 dB of the reference level, the run notes that noise peaks may have been clipped.
- **Hot wire**: the bias for the "on" state (default 0.1 V); "off" is 0 V with the output on, as in the hot-wire calibration. Set up like the Y factor measurement (DC; an output already on must be in DC mode).
- **TWPA pump**: frequency and power (SynthHD channel B), within the rf_synth limits; set with the output off, then toggled.
- **Qubit flux bias**: set for the run (default, 0 V, as the hot-wire calibration) or left as it is; reserved either way, its state saved.
- Before: the SMW200A probe off, warm switches `fridge_in` → rf_probe, `rx_source` → fridge, `receiver` → spa, the flux bias, the SA, the pump (off, frequency and power), then the hot wire at 0 V. After: restore everything as it was / leave everything at the last state / turn the noise source (at 0 V) and the pump off, and the flux bias if the run set it.
- **Result**: the four spectra (each averaged over the rounds so far), the hot-wire Y with the pump off and on, Y = P(on) / P(0 V), and the TWPA gain, [P(on, on) − P(on, 0)] / [P(off, on) − P(off, 0)] in dB (the hot wire's step through the chain with the pump on over the same with it off, so the hot-wire temperature cancels; with a four-wave TWPA it also holds the hot wire's noise at the idler $2f_p - f$, so it reads above the TWPA gain: the TWPA VNA gain measures that alone), all vs frequency with an optional running mean; hover reads every curve at a frequency.
- Saved JSON: `frequency_hz`, per spectrum `round`, `pump`, `hot_wire_v`, `traces`, `sweep_start_time`/`sweep_end_time`, `mean_dbm`, `max_dbm` and `traces_w` [spectrum][401]; `power_w` {state: [401]} (each state averaged over the rounds), `y_pump_off`, `y_pump_on`, `gain_db` [401], `hot_wire_on_v`, `pump_frequency_hz`, `pump_power_dbm`, `pump_state` (once on), `flux_bias_state`, `spa_setup`, `fridge_temperature_k`, `params`, `instruments`, `status`, `start_time`, `finish_time`, `sweep_time`.

### TWPA VNA gain (`twpa_vna_gain`)
Measures the TWPA gain at every frequency: **VNA S21** over a range (default 5.5–6.5 GHz, 401 points: the frequencies of the TWPA noise spectrum's defaults) with the **TWPA pump off and on**, repeated in rounds, $G = |S_{21}(\mathrm{on}) / S_{21}(\mathrm{off})|^2$. The S21 is coherent, so unlike the hot-wire gain of the TWPA noise spectrum it has no idler part: it is the gain of the TWPA alone.
- **Rounds**: rounds, traces per state per round (single-sweep complex traces, averaged), wait after the pump goes on / off. Each round takes off then on, every other round in reverse, so the pump toggles once per round and slow drifts cancel. Each state is the complex mean over all rounds.
- **VNA**: start, stop, points, IFBW, power (default −10 dBm: keep the TWPA linear with the pump on; check with the TWPA signal sweep's power axis), the warm step attenuator left as it is or set. The plan check refuses a range that contains the pump.
- **TWPA pump**: frequency and power (SynthHD channel B), within the rf_synth limits; set with the output off, then toggled.
- **Qubit flux bias**: set for the run (default, 0 V) or left as it is; reserved either way, its state saved.
- Before: warm switches `fridge_in` → vna, `rx_source` → fridge, `receiver` → vna, the attenuator, the flux bias, the pump (off, frequency and power), the VNA. After: restore everything as it was / leave everything at the last state / turn the VNA RF and the pump off, and the flux bias if the run set it.
- **Result**: |S21| with the pump off and on (each averaged over the rounds so far) and the gain, vs frequency, with an optional running mean.
- Saved JSON: `frequency_hz`, per state `round`, `pump`, `averages`, `point_start_time`/`point_end_time`, `mean_db`, `s_real`/`s_imag` [state][points]; `s21_off_real`/`_imag`, `s21_on_real`/`_imag` (the means over the rounds), `gain_db`, `vna_power_dbm`, `pump_frequency_hz`, `pump_power_dbm`, `pump_state` (once on), `attenuator_state`, `flux_bias_state`, `fridge_temperature_k`, `params`, `instruments`, `status`, `start_time`, `finish_time`, `sweep_time`.

## Settings tab
The **settings** tab ([labserver/settings.py](../labserver/settings.py)) changes, while the server runs, settings that would otherwise need an edit of `config.yaml` and a restart:

| Section | What |
|---|---|
| Instrument limits | `[min, max]` for every numeric setting a driver offers: SA center/span/RBW/VBW/ref level (dBm)/sweep time/input attenuation, VNA start/stop/CW/points/IFBW/power, SynthHD pump and SMW200A probe frequency and power, GS210 level in VOLT mode (V) and CURR mode (mA) plus **allow current mode**, 33220A DC offset, RCDAT attenuation. An empty box is no bound; a limit config.yaml doesn't set starts empty. |
| Live view | longest sweep shown live, read attempts per frame, settings re-read interval, largest average count, frame-rate cap per instrument |
| Instrument I/O | I/O timeout per instrument, SA/VNA trace read attempts and wait between them, SA wait after each zero-span read, GS210 ramp step and wait |
| Experiments | points per sweep (2000), points per run (10000), data file size warning (200 MB) |
| Outputs off | how long to wait for a busy instrument before turning it off anyway (3 s) |
| Experiment defaults | which experiments have saved defaults; put them back to built-in |

- Edited fields are marked `*`; **save** (or Enter) sends them together, and either all are applied or none. They apply at once: limits at the next command, live settings at the next frame, I/O settings at the next command (a read already running keeps its timeout).
- **Loosening a limit** (a lower min, a higher max, a bound removed) or turning **allow current mode** on asks for a confirmation that lists every loosened item; the server refuses it without that (409 `confirm`) and logs it as a warning. Tightening applies without asking.
- Changes are saved in `settings.local.yaml` next to `config.yaml` and applied over it at every start. The file is in `.gitignore`, so a `git push` deploy keeps it. `config.yaml` stays the base: a row changed in the tab shows the config.yaml value and a **back to config** button, and a value set back to the config.yaml value is dropped from the file. Deleting the file goes back to config.yaml entirely; a file that can't be read is moved aside (`settings.local.yaml.broken-<time>`) and the server starts from config.yaml.
- Not in the tab, still in `config.yaml`: wiring (VISA `match`, IPs, COM port, switch names and positions), `init_commands`, the server address and the data folder.
- **The SA reference level limit is in dBm** whatever the SA unit: a value in W, V, dBmV or dBµV is converted (50 Ω) before the check. Within one apply, the unit (and the scale) is set before the reference level, so `{"unit": "W", "ref_level": 2e-8}` means 2e-8 W.
- The SA input attenuation does not follow the reference level here: `init_commands` sends `:POW:ATT 0`, which sets it by hand to 0 dB (auto coupling off). Its limit is offered separately, empty by default.
- **GS210 current mode**: the level means volts in VOLT mode and amps in CURR mode, so the driver checks the mode (the one the same apply sets, else the instrument's). With **allow current mode** off (the default), switching to CURR is refused, and so are level changes and turning the output on while the source is in CURR (e.g. left so on the front panel); switch it to VOLT first. With it on, the CURR-mode (mA) limit applies.

## Server
```bash
run-server.bat --sim
```
Without `--sim` it connects to the real instruments; then open `http://<its address>:8000` (see [Network access](../README.md#network-access)). Besides the web UI at `/`, the raw API can be tried out at `/docs`.

| Method | Path | Description |
|---|---|---|
| GET | `/api/status` | Connection state, busy owner and errors for every instrument, the last SA alignment (`align`), plus `turning_off` (outputs off is waiting for a running experiment to stop) |
| GET | `/api/instruments` | Kind, settable parameters, limits and switch names for each instrument |
| GET | `/api/{instrument}/state` | Current state |
| POST | `/api/{instrument}` | Set values. Example bodies: `{"receiver": "spa"}`, `{"pump_power_dbm": 7}` |
| POST | `/api/{instrument}/align` | Start the instrument's own alignment in the background (SA only); progress and result in `/api/status` under `align` |
| POST | `/api/{instrument}/reconnect` | Reconnect |
| POST | `/api/{instrument}/scpi` | Raw command, e.g. `{"command": "*IDN?"}`. Bypasses limits, so every use is logged |
| POST | `/api/outputs_off` | Turn every output off; the server keeps running |
| POST | `/api/shutdown` | Stop the server; outputs stay as they are |
| GET | `/api/settings` | Every setting the settings tab offers: value, config.yaml value, whether it is overridden |
| POST | `/api/settings` | `{"changes": {id: value}, "reset": [id], "confirm": false}`; all or nothing. Loosening without `confirm` returns 409 `confirm` with the list in `loosened` |
| GET | `/api/experiments` | Every predefined experiment and its settings |
| PUT / DELETE | `/api/experiments/{id}/defaults` | Save the given settings as the experiment's defaults / back to built-in |
| POST | `/api/experiments/{id}/plan` | Check settings and list the sweep points; changes no instrument |
| POST | `/api/experiments/{id}/start` | Check settings (as plan) and start the run; 409 if one is running |
| GET | `/api/jobs/current?since=k` | The running or last job: status, progress, and result rows from `k` on; `url` / `html_url` of its saved record once it is saved |
| POST | `/api/jobs/{id}/stop` | Stop after the trace being read; the points so far are saved |

Live view:

| Method | Path | Description |
|---|---|---|
| WS | `/ws/live/{spa\|vna}` | Streams `{"type": "frame", ...}` (latest only) and `{"type": "status", ...}` (at least every 0.5 s) |
| GET | `/api/live` | Status, fps and number of viewers for each live view |
| POST | `/api/live/{name}/hold` | `{"hold": true}` freezes the last frame (Run/Hold) |
| POST | `/api/live/{name}/average` | `{"count": 16}` running average of the last 16 traces (1 = off); `{"restart": true}` starts it over |
| POST | `/api/live/{name}/save` | `{"label": "...", "note": "...", "seq": 123}` saves frame `seq` (the one on screen; the UI sends it) plus a snapshot of every instrument; without `seq`, the latest trace. Returns `url` for the download |
| GET | `/api/data/{DD-MM-YYYY}/{file}` | Downloads a saved JSON record; `<name>.html` for `<name>.json`: the interactive HTML page of an experiment record (404 for other records) |
| GET | `/static/{file}` | The UI's script and style files (`results.js`, `results.css`) |

The live view reads only while someone is watching, and stops reading when they leave.
When it stops, it hands the front panel back (the SA returns to continuous sweep).
It pauses while a job has claimed the instrument, while outputs off stops a job, and on hold,
and it yields to any other request for the instrument, so UI clicks don't wait.
SA frames carry `y` in the SA's unit; VNA frames carry `y_db` and `y_deg`. Saved records hold the complex data (`y_real`, `y_imag`).
Frame-rate caps are in the settings tab (`live` in `config.yaml`).
A live read holds the instrument for the whole sweep, so the live view doesn't start a sweep longer than `live.max_sweep_s` (100 s in config.yaml; 5 s if unset); for example, RBW 1 kHz over the full span would take hours. It shows "paused: sweep time ... > live limit" instead, and settings can still be changed.
The sweep time counts averaging: SA average count (when averaging is on) and VNA sweep count.
One frame appears per finished sweep, however slow the instrument; when it is faster than the network or the browser, frames in between are skipped (latest only), never queued.
A failed live read is retried `live.max_retries` (2) times, not the SA's 50; then the status shows the error and the live view tries again a second later, leaving the instrument free in between.
Settings are cached for the x axis and re-read every `live.state_refresh_s` (5 s), after each raw SCPI command, and when the VNA returns a different number of points. A change made on the front panel can show the old x axis for up to that long.
"save trace" stores exactly the frame on screen (by `seq`). The server keeps the last 64 frames; if the frame is older than that, the save is refused rather than storing a different trace (press hold, then save).

Errors come back as `{"error": code, "detail": ...}`:

| Status | Code | Meaning |
|---|---|---|
| 400 | `instrument_error` | A limit was exceeded or a value was invalid |
| 400 | `invalid_settings` | Experiment or settings-tab settings with an unknown key or a value of the wrong type |
| 409 | `busy` | Another user holds the instrument |
| 409 | `confirm` | A settings change loosens a limit; send it again with `"confirm": true` |
| 423 | `outputs_off` | Outputs off is waiting for a running experiment to stop; try again in a moment |
| 502 | `io_error` / `measurement_failed` | Communication or measurement failed |
| 503 | `unavailable` | The instrument is not connected or does not exist |

Stopping the server with Ctrl-C leaves the outputs as they are.

## Output policy (InstrumentManager)
| Situation | Outputs |
|---|---|
| Server start / instrument reconnect | **Untouched**: the current state is read and taken over. A test checks that no OUTP command is written while connecting. |
| Outputs off | **All off**; the server keeps running. A running job stops at its next check and skips its "when done" step; until it has, new commands are refused (423) and the live view pauses. |
| Shutdown, Ctrl-C | Only closes the connections. The pump, flux bias and so on stay as they are. |

Outputs off turns all instruments off **in parallel**, so a busy instrument (e.g. the VNA in a long sweep) doesn't delay the others. For an instrument held by a job, it sends the off command anyway after `off_lock_timeout_s` (3 s, settings tab).
If one instrument fails to connect, the others keep working. `status()` shows each instrument's error and who is using it.

## Notes
- **SMW200A (probe)**: only the CW frequency (`:SOUR1:FREQ:CW`), the level (`:SOUR1:POW:LEV:IMM:AMPL`) and RF on/off (`:OUTP1:STAT`) are set; RF off goes first and RF on last within one `apply()`. Modulation, IQ, the frequency/level modes and offsets, ALC and the reference oscillator (`reference_source`, INT/EXT) are only read. The lab's unit has one RF path (SMW-B1044: 100 kHz-44 GHz, -145..+30 dBm settable; the default probe power limit is -145..0 dBm). Like the E7405A it doesn't answer an unsupported query (the read times out), so only queries proven on it are used. After every exchange it gets USB488 GO_TO_LOCAL, so its touchscreen works between commands.
- **SynthHD**: only channel B (the TWPA pump) is named; channel A was the probe until 2026-10-19. Outputs off still turns both channels off.
- **Windfreak: `synth.init()` is never called.** The library's `init()` forces the internal 27 MHz reference and resets both channels to minimum frequency and power. To force a reference, set `reference_mode` in `config.yaml`.
- **SA single sweep**: reading a trace sets `INIT:CONT OFF`, so only a sweep that started after the last setting change is read. `close()` returns the SA to continuous mode.
  The notebooks contain the same idea, commented out: `#spa.write(':INIT:CONT OFF')  # Single Sweep Mode (Stops mid-sweep data corruption)`.
- **SA commands on connect** (`init_commands`): from the notebook's "fix defaults" cell: `:AVER:TYPE POW`, `:CAL:AUTO OFF`, `:POW:ATT 0`.
- **VNA**: the trace read is the notebooks' sequence (`INIT:CONT OFF`, `INIT:IMM`, `*OPC?`, `CALC1:DATA? SDAT`). The VNA stays in single sweep while traces are read and goes back to continuous sweep and local (front panel) once reading stops; switching after every live frame made its screen flash.
- **pyvisa-py over USB (Linux)**: [labserver/usbtmc_patch.py](../labserver/usbtmc_patch.py) patches pyvisa-py 0.8.1 at startup. Without it, any response longer than 1000 bytes through a GpibUsb adapter (every SA or VNA trace) hangs until the timeout, and RsInstrument leaves the newline after a VNA binary block unread. go_to_local on the VNA sends the USB488 GO_TO_LOCAL request, because the `&GTL` text RsInstrument would send is rejected with -113.
- **The GS210 is fixed as the qubit flux bias.** To use it for the noise diode, recable it and change only its level limit (settings tab or `config.yaml`, see the comment there).
  - Turning the output on also checks the level already set in the instrument against the limits, so a 28 V level left over from noise-diode use can't reach the flux line.
  - Within one `apply()`, turning off happens first, turning on happens last, and the range is set before the level.
  - Current mode (CURR) is refused unless **allow current mode** is on in the settings tab (see [Settings tab](#settings-tab)).
  - Level changes while the output is on are split into `ramp_step` steps (default 0.05 V).
- **Switches** are set by name (`apply(receiver="spa")`). Channels that aren't named keep their position. The state is read back after every write to confirm it.
