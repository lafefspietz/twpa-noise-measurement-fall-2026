#!/usr/bin/env bash
# One-time system setup for running the lab server on Ubuntu (pyvisa-py, no NI-VISA).
# Run once with sudo:  sudo bash scripts/linux-setup.sh
# Safe to run again.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "run with sudo"; exit 1; fi
USER_NAME="${SUDO_USER:?run with sudo from the lab user account}"

echo "== Packages =="
# python3-venv for the .venv. tmux keeps the server running after an SSH session closes.
# chrony keeps the clock synced (data file names use the date and epoch time).
apt-get install -y python3-venv tmux chrony
timedatectl set-ntp true

echo "== USB access for GpibUsb adapters (03EB:2065) =="
# pyvisa-py talks to the adapters through libusb and detaches the kernel usbtmc driver
# itself, so the user only needs write access to the USB device.
cat > /etc/udev/rules.d/70-labserver.rules <<'EOF'
# GpibUsb USB-GPIB adapters (USBTMC), one per GPIB instrument
SUBSYSTEM=="usb", ATTRS{idVendor}=="03eb", ATTRS{idProduct}=="2065", GROUP="plugdev", MODE="0660", TAG+="uaccess"
EOF
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb

echo "== Serial port access (Windfreak SynthHD on /dev/ttyACM*) =="
usermod -aG dialout,plugdev "$USER_NAME"
# ModemManager probes new ttyACM devices and can grab the SynthHD; this laptop has no modem.
systemctl disable --now ModemManager 2>/dev/null || true

echo "== Keep running with the lid closed, never suspend =="
mkdir -p /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/50-labserver.conf <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
systemctl kill -s HUP systemd-logind
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

echo "== Instrument LAN (USB Ethernet on the hub, Mini-Circuits at 169.254.x.x) =="
# Not tied to an interface name, so it applies to the hub's USB LAN adapter whenever it
# is plugged in. never-default keeps internet traffic on Wi-Fi.
if ! nmcli -t -f NAME connection show | grep -qx lab-instruments; then
  nmcli connection add type ethernet con-name lab-instruments \
    ipv4.method manual ipv4.addresses 169.254.1.10/16 ipv4.never-default yes \
    ipv6.method disabled connection.autoconnect yes
fi

echo "== Let the user's systemd services run without a login session =="
loginctl enable-linger "$USER_NAME"

echo
echo "Done. Log out and back in (or reconnect SSH) for the dialout/plugdev groups to apply."
