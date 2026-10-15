# Remote Experiment Server

**English** | [한국어](README.ko.md)

Made by Youngdu Kim.

One computer is connected to every instrument and runs this server. Anyone on the network opens a web page to set the instruments, watch live SA/VNA traces, and run predefined sweeps. The results are saved as JSON, in the same layout the notebooks use.

## Using it
Open `http://<server>:8000` in a browser (see [Network access](#network-access)). The page has three tabs:
- **instruments**: the live SA or VNA trace (hold, running average, **save trace**), the settings of the selected instrument, and knobs for the TWPA pump, the probe signal, the qubit flux bias, the noise source, the attenuator and the warm switches. Turn a knob with the mouse wheel, or type a value and press Enter. The SA view also shows its alignment status, with a **do align** button (about 3-6 min; without a cable from AMPTD REF OUT to INPUT only the filters are aligned, not RF). The page reads the instruments when it loads and when you press **refresh**; it does not poll them.
- **experiments**: qubit flux sweep, qubit power sweep and Y factor measurement. Fill in the form; the server checks it as you type (number of points, time, errors and warnings) without touching any instrument. **start** runs it, one experiment at a time; the instruments it uses are reserved until it ends. **stop** ends it after the current trace, and the points measured so far are still saved.
- **settings**: instrument limits, live-view caps, I/O timeouts and experiment caps. Loosening a limit asks for a confirmation.

Safety:
- **outputs off** turns every output off (pump, signal, flux bias, VNA RF, hot wire) and keeps the server running. A running experiment stops, without its "when done" step.
- **shut down server** stops the server and leaves the outputs as they are, as Ctrl-C does. To have everything off, press **outputs off** first. Starting or restarting the server never changes an output either.
- Every value is checked against the limits before it is sent.

Saved data: `data/DD-MM-YYYY/<epoch>-<label>.json` on the server. **save trace** also puts a copy on your computer, and a finished experiment shows a download link. Every record has a snapshot of all instrument settings at the time.

## Instruments
| Name (`config.yaml`) | Instrument | Link | Driver |
|---|---|---|---|
| `spa` | HP E7405A spectrum analyzer | USB-GPIB (VISA) | `e7405a.py` |
| `vna` | R&S ZVA24 | USB-GPIB (RsInstrument) | `zva.py` |
| `rf_synth` | Windfreak SynthHD (probe = A, pump = B) | USB serial (windfreak package) | `windfreak.py` |
| `qubit_flux_bias` | Yokogawa GS210 | USB-GPIB (VISA) | `gs210.py` |
| `noise_source_bias` | Agilent 33220A (DC, hot wire) | USB-GPIB (VISA) | `fgen33220a.py` |
| `warm_switches` | Mini-Circuits RC-4SPDT-A18 | Ethernet HTTP | `rc4spdt.py` |
| `attenuator` | Mini-Circuits RCDAT-44G-63 | Ethernet HTTP | `rcdat.py` |

Each GPIB instrument has its own USB-GPIB adapter (GpibUsb, USB 03EB:2065, USBTMC). The switch box and the attenuator are on a USB LAN adapter at 169.254.x.x.

## Installing the server
### Linux (recommended)
pyvisa-py talks to the USB-GPIB adapters directly, so no NI-VISA is needed.
1. One-time system setup: USB and serial port access, no suspend with the lid closed, the instrument LAN (169.254.1.10/16), tmux and chrony. Log out and back in afterwards.
   ```bash
   sudo bash scripts/linux-setup.sh
   ```
2. In this folder, install the packages:
   ```bash
   python3 -m venv .venv
   ```
   ```bash
   .venv/bin/pip install -r requirements.txt
   ```
3. Check the install with the simulators (no instruments needed):
   ```bash
   .venv/bin/python -m pytest -q
   ```
4. Plug in the instruments and close every other program that uses them (e.g. Jupyter kernels with open VISA sessions). The SynthHD port can only be open in one program at a time.
5. Check `config.yaml`: the instrument `match` strings, the switch and attenuator IPs, and the `limits`.
6. Start the server in the background (output goes to `logs/stdout.log`):
   ```bash
   scripts/restart-server.sh --host all
   ```
   `./run-server.sh` runs it in the foreground instead. With `--sim` it runs against simulators.

### Windows
Install **NI-VISA** and keep the USB-GPIB adapters on its USBTMC driver ("USB Test and Measurement Devices" in Device Manager; don't use Zadig/WinUSB). Then:
```bash
python -m venv .venv
```
```bash
.venv\Scripts\pip install -r requirements.txt
```
```bash
run-server.bat
```
Set the network to **Private**, allow Python through Windows Firewall when asked, and turn off sleep and USB selective suspend.

## Network access
`server.host` in `config.yaml`, or `--host` on the start command:

| Setting | Who can connect |
|---|---|
| `all` (default) | anyone on the same LAN, Tailscale devices, and this machine |
| `tailscale` | only devices on your tailnet |
| `local` | only this machine |

There is **no login**: anyone who can reach the server can drive the instruments. That is fine on a lab network but not on a shared institutional one, where Wi-Fi may also block device-to-device traffic. Then use [Tailscale](https://tailscale.com): `http://<Tailscale name>:8000`, or `https://<name>.<tailnet>.ts.net` after running `tailscale serve --bg 8000` once on the server. Saving traces straight into a folder of your choice ("choose folder") needs HTTPS (or localhost) and Chrome or Edge.

## Checking the instruments
`scripts/bringup.py` checks one instrument at a time: connect, identify, read the state, write the current values back unchanged, and read traces. It never turns an output on.
```bash
.venv/bin/python scripts/bringup.py all --sim
```
```bash
.venv/bin/python scripts/bringup.py spa -v
```
- `--no-reapply` only reads.
- `--zero-span` (SA) also applies the notebooks' zero-span settings and measures the power.
- `--exercise` (switches) moves each switch through every position, then restores it.

## More
[docs/REFERENCE.md](docs/REFERENCE.md) (English) has the details: code layout, every part of the web UI, what each experiment does before and after the sweep, the fields in the saved JSON, the settings tab, the HTTP API (also at `/docs` on the server) and the output policy.
