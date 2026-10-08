#!/usr/bin/env bash
# One command for the laptop (macOS / Linux / WSL2), option (a) in README.md: the laptop
# is the reachable relay, and also runs a worker and a requester.
#   ./spike/p2p/run_laptop.sh
# Same behaviour as run_laptop.ps1: relay on 0.0.0.0:4001 (+UPnP attempt), worker
# "laptop-echo", requester waiting up to 15 min for the pod's "pod-echo"; stays up
# 30 minutes, Ctrl+C stops earlier. A UPnP mapping it created is removed on exit.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-4001}"
[ -x .venv/bin/python ] || { echo "First run: installing py-libp2p ..."; ./install.sh; }

if [ "$(uname -s)" = Darwin ]; then
  LAN="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '?')"
else
  LAN="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
echo
echo "This machine's LAN address: ${LAN:-?}   (port-forward TCP $PORT to it if UPnP fails)"
echo "macOS may ask whether Python may accept incoming connections: allow it."
echo "WSL2: the relay is inside a VM; see README (WSL2 needs mirrored networking)."
echo "When the relay prints its peer id, send Claude:  <your public IP>  and that peer id."
echo
.venv/bin/python -u p2p_spike.py relay+worker+requester --host 0.0.0.0 --port "$PORT" --upnp \
  --model laptop-echo --want pod-echo --wait 900 --lifetime 1800 2>&1 | tee laptop-run.log
