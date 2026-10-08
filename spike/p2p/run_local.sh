#!/usr/bin/env bash
# Local test: relay, two workers and two requesters as SEPARATE processes on 127.0.0.1.
# Asserts that a requester finds each worker in the DHT, reaches it through the relay,
# and gets a reply. Scenario 1 is "private" mode (relay only); scenario 2 is "direct"
# mode (hole-punch upgrade allowed). Every process started here is stopped on exit.
#
#   ./run_local.sh            (uses .venv/ next to this script if present, else python3)
#   PORT=4101 ./run_local.sh  (pick another relay port)
set -u
cd "$(dirname "$0")"
PY="${PYTHON:-}"
[ -z "$PY" ] && [ -x .venv/bin/python ] && PY=.venv/bin/python
[ -z "$PY" ] && PY=python3
PORT="${PORT:-4101}"
LOGS="$(mktemp -d "${TMPDIR:-/tmp}/dai-p2p-local.XXXXXX")"
PIDS=()

cleanup() {
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null; done
  sleep 1
  for pid in "${PIDS[@]}"; do kill -9 "$pid" 2>/dev/null; done
}
trap cleanup EXIT

start() {  # start <logname> <args...>  -- runs one role in the background
  local name="$1"; shift
  "$PY" -u p2p_spike.py "$@" >"$LOGS/$name.log" 2>&1 &
  PIDS+=("$!")
}

fail=0
check() {  # check <logname> <regex> <description>
  if grep -Eq "$2" "$LOGS/$1.log"; then echo "  PASS  $3"; else echo "  FAIL  $3"; fail=1; fi
}

echo "logs: $LOGS"
echo "starting relay on 127.0.0.1:$PORT ..."
start relay relay --port "$PORT" --upnp
for _ in $(seq 1 30); do grep -q '^RELAY_ADDR' "$LOGS/relay.log" && break; sleep 0.5; done
RELAY="$(grep -oP '^RELAY_ADDR \K\S+' "$LOGS/relay.log" || true)"
[ -n "$RELAY" ] || { echo "relay did not start"; cat "$LOGS/relay.log"; exit 1; }
echo "relay: $RELAY"

start worker_private worker --relay "$RELAY" --model qwen-7b --privacy private
start worker_direct  worker --relay "$RELAY" --model llama-3.1-8b --privacy direct
for _ in $(seq 1 40); do
  grep -q 'dht_provide OK' "$LOGS/worker_private.log" && grep -q 'dht_provide OK' "$LOGS/worker_direct.log" && break
  sleep 0.5
done

echo "scenario 1: private mode (relay only) ..."
"$PY" -u p2p_spike.py requester --relay "$RELAY" --want qwen-7b --privacy private \
  >"$LOGS/requester_private.log" 2>&1
echo "scenario 2: direct mode (upgrade allowed) ..."
"$PY" -u p2p_spike.py requester --relay "$RELAY" --want llama-3.1-8b --privacy direct \
  >"$LOGS/requester_direct.log" 2>&1
sleep 1

for f in relay worker_private worker_direct requester_private requester_direct; do
  echo; echo "===== $f ====="; cat "$LOGS/$f.log"
done

echo; echo "===== checks ====="
check worker_private    'RESULT reservation OK'          "private worker reserved a relay slot"
check worker_private    'RESULT dht_provide OK'          "private worker advertised its model in the DHT"
check requester_private 'RESULT dht_lookup OK'           "requester found the private worker via the DHT"
check requester_private 'RESULT relayed_connection OK'   "requester reached it through the relay"
check requester_private 'RESULT message OK over RELAYED' "message + reply went over the relay"
# IP privacy: in private mode a peer's only direct socket may be the one to its relay.
RID="${RELAY##*/}"; RSHORT="${RID:0:8}..${RID: -6}"
for f in worker_private requester_private; do
  if grep 'direct socket' "$LOGS/$f.log" | grep -vq "$RSHORT"; then
    echo "  FAIL  $f had a direct socket to a peer other than the relay"; fail=1
  else
    echo "  PASS  $f's only direct socket was to the relay (no other peer saw its address)"
  fi
done
check requester_direct  'RESULT dht_lookup OK'           "requester found the direct-mode worker via the DHT"
check requester_direct  'RESULT relayed_connection OK'   "requester reached it through the relay"
check requester_direct  'RESULT message OK'              "message + reply succeeded"
grep -h 'RESULT dcutr_upgrade' "$LOGS/requester_direct.log" | sed 's/^/  INFO  /'
grep -h 'RESULT upnp' "$LOGS/relay.log" | sed 's/^/  INFO  /'
echo
if [ "$fail" = 0 ]; then echo "LOCAL TEST PASSED"; else echo "LOCAL TEST FAILED"; fi
exit "$fail"
