# Remote Experiment Server

[English](README.md) | **한국어**

만든 사람: 김영두

컴퓨터 한 대가 모든 장비에 연결되어 이 서버를 돌린다. 네트워크에 있는 누구나 웹 페이지를 열어 장비를 설정하고, SA/VNA trace를 실시간으로 보고, 미리 정의한 sweep을 실행할 수 있다. 결과는 notebook과 같은 형식의 JSON으로 저장된다.

## 사용법
브라우저에서 `http://<서버>:8000`을 연다 ([네트워크 접속](#네트워크-접속) 참고). 페이지에는 탭이 세 개 있다:
- **instruments**: SA 또는 VNA 실시간 trace (hold, 이동 평균, **save trace**), 선택한 장비의 설정, 그리고 TWPA pump, probe 신호, qubit flux bias, noise source, 감쇠기, warm 스위치를 조절하는 노브. 노브는 마우스 휠로 돌리거나 값을 입력하고 Enter를 누른다. SA 화면에는 정렬(alignment) 상태와 **do align** 버튼이 있다 (약 3~6분; AMPTD REF OUT → INPUT 케이블이 없으면 RF는 건너뛰고 필터만 정렬한다). 페이지는 열 때와 **refresh**를 누를 때만 장비를 읽고, 주기적으로 읽지 않는다.
- **experiments**: qubit flux sweep, qubit power sweep (VNA로, 또는 SMW200A probe와 SA로), Y factor measurement, TWPA pump sweep (SA로 pump 주파수·파워에 따른 gain과 SNR improvement 측정), TWPA signal sweep (pump를 고정하고 signal 주파수 또는 파워에 따른 SNR 측정, noise source bias도 함께 스윕 가능), TWPA noise spectrum (hot wire 0 V/on, pump off/on 상태에서 넓은 SA 스펙트럼을 받아 주파수마다 hot-wire Y와 gain 측정), TWPA VNA gain (pump off/on에서 VNA S21을 받아 주파수마다 TWPA gain 측정). 입력하는 동안 서버가 설정을 검사한다 (포인트 수, 시간, 오류, 경고). 이때 장비는 건드리지 않는다. **start**로 실행하며 한 번에 하나만 돈다. 실행 중에는 쓰는 장비가 예약된다. **stop**은 지금 읽는 trace가 끝나면 멈추고, 그때까지 측정한 포인트는 저장된다.
- **settings**: 장비 limits, live view 상한, I/O timeout, 실험 상한. limit을 넓힐 때는 확인을 받는다.

안전:
- **outputs off**는 모든 출력(pump, signal, flux bias, VNA RF, hot wire)을 끄고, 서버는 계속 돈다. 실행 중인 실험은 멈추고 "when done" 단계는 건너뛴다.
- **shut down server**는 서버를 끄고 출력은 그대로 둔다 (Ctrl-C도 같다). 다 끄고 싶으면 **outputs off**를 먼저 누른다. 서버를 시작하거나 재시작해도 출력은 바뀌지 않는다.
- 모든 값은 보내기 전에 limits로 검사한다.

저장 데이터: 서버의 `data/DD-MM-YYYY/<epoch>-<label>.json`. **save trace**는 내 컴퓨터에도 사본을 저장하고, 끝난 실험에는 다운로드 링크가 나온다. 모든 기록에는 그 시점의 장비 설정 스냅샷이 들어 있다.

끝난 실험에는 **download interactive HTML** 링크도 있다: 어떤 브라우저에서든 오프라인으로 열리는 HTML 파일 하나로, experiments 탭과 같은 그림(마우스를 올리면 값이 보이고, TWPA pump/signal sweep은 각 포인트의 spectrum과 값까지), 실험의 모든 설정, 모든 장비의 상태가 들어 있다. 다른 곳에 복사해 둔 기록 JSON은 `python -m labserver.report <기록.json>`으로 그 옆에 같은 페이지를 만든다.

## 장비
| 이름 (`config.yaml`) | 장비 | 연결 | 드라이버 |
|---|---|---|---|
| `spa` | HP E7405A spectrum analyzer | USB-GPIB (VISA) | `e7405a.py` |
| `vna` | R&S ZVA24 | USB-GPIB (RsInstrument) | `zva.py` |
| `rf_probe` | R&S SMW200A (probe / signal 톤, RF A) | USB-GPIB (VISA) | `smw200a.py` |
| `rf_synth` | Windfreak SynthHD (TWPA pump = B, A는 미사용) | USB 시리얼 (windfreak 패키지) | `windfreak.py` |
| `qubit_flux_bias` | Yokogawa GS210 | USB-GPIB (VISA) | `gs210.py` |
| `noise_source_bias` | Agilent 33220A (DC, hot wire) | USB-GPIB (VISA) | `fgen33220a.py` |
| `warm_switches` | Mini-Circuits RC-4SPDT-A18 | Ethernet HTTP | `rc4spdt.py` |
| `attenuator` | Mini-Circuits RCDAT-44G-63 | Ethernet HTTP | `rcdat.py` |

GPIB 장비는 각각 USB-GPIB 어댑터(GpibUsb, USB 03EB:2065, USBTMC)로 연결된다. 스위치 박스와 감쇠기는 169.254.x.x의 USB LAN 어댑터에 있다.

## 서버 설치
### Linux (권장)
pyvisa-py가 USB-GPIB 어댑터와 직접 통신하므로 NI-VISA가 필요 없다.
1. 시스템 최초 설정: USB/시리얼 포트 권한, 덮개를 닫아도 절전 안 함, 장비 LAN(169.254.1.10/16), tmux, chrony. 끝나면 로그아웃 후 다시 로그인한다.
   ```bash
   sudo bash scripts/linux-setup.sh
   ```
2. 이 폴더에서 패키지를 설치한다:
   ```bash
   python3 -m venv .venv
   ```
   ```bash
   .venv/bin/pip install -r requirements.txt
   ```
3. 시뮬레이터로 설치를 확인한다 (장비 필요 없음):
   ```bash
   .venv/bin/python -m pytest -q
   ```
4. 장비를 연결하고, 장비를 쓰는 다른 프로그램(VISA를 열어둔 Jupyter 커널 등)은 모두 종료한다. SynthHD 포트는 한 프로그램만 열 수 있다.
5. `config.yaml` 확인: 장비 `match` 문자열, 스위치와 감쇠기 IP, `limits`.
6. 서버를 백그라운드로 실행한다 (출력은 `logs/stdout.log`):
   ```bash
   scripts/restart-server.sh --host all
   ```
   `./run-server.sh`는 터미널에서 바로 실행한다. `--sim`을 붙이면 시뮬레이터로 돈다.

### Windows
**NI-VISA**를 설치하고 USB-GPIB 어댑터는 그 USBTMC 드라이버로 둔다 (장치 관리자의 "USB Test and Measurement Devices"; Zadig/WinUSB는 쓰지 말 것). 그 다음:
```bash
python -m venv .venv
```
```bash
.venv\Scripts\pip install -r requirements.txt
```
```bash
run-server.bat
```
네트워크를 **개인(Private)**으로 두고, 물어보면 Windows 방화벽에서 Python을 허용하고, 절전과 USB 선택적 일시 중단을 끈다.

## 네트워크 접속
`config.yaml`의 `server.host` 또는 실행 명령의 `--host`로 설정한다:

| 설정 | 접속 가능한 곳 |
|---|---|
| `all` (기본) | 같은 LAN의 누구나, Tailscale 기기, 이 컴퓨터 |
| `tailscale` | 내 tailnet 기기만 |
| `local` | 이 컴퓨터만 |

**로그인이 없다**: 서버에 접속할 수 있는 사람은 누구나 장비를 조작할 수 있다. 연구실 전용 네트워크면 괜찮지만 기관 공용 네트워크에서는 주의해야 하고, 그런 Wi-Fi는 기기 간 통신을 막기도 한다. 그때는 [Tailscale](https://tailscale.com)을 쓴다: `http://<Tailscale 이름>:8000`, 또는 서버에서 `tailscale serve --bg 8000`을 한 번 실행한 뒤 `https://<이름>.<tailnet>.ts.net`. 원하는 폴더에 trace를 바로 저장하는 기능("choose folder")은 HTTPS(또는 localhost)와 Chrome/Edge에서만 된다.

## 장비 점검
`scripts/bringup.py`는 장비를 하나씩 점검한다: 연결, 식별, 상태 읽기, 현재 값을 그대로 다시 쓰기, trace 읽기. 출력은 절대 켜지 않는다.
```bash
.venv/bin/python scripts/bringup.py all --sim
```
```bash
.venv/bin/python scripts/bringup.py spa -v
```
- `--no-reapply`: 읽기만 한다.
- `--zero-span` (SA): notebook의 zero-span 설정도 적용하고 파워를 측정한다.
- `--exercise` (스위치): 모든 위치로 돌려본 뒤 원래대로 되돌린다.

## 더 보기
[docs/REFERENCE.md](docs/REFERENCE.md) (영문)에 자세한 내용이 있다: 코드 구조, 웹 UI의 각 부분, 각 실험이 sweep 전후에 하는 일, 저장 JSON의 필드, settings 탭, HTTP API(서버의 `/docs`에서도 볼 수 있음), 출력 정책.
