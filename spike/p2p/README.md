# P2P networking spike (py-libp2p)

This is the "First milestone" spike from [docs/DECENTRALIZATION.md](../../docs/DECENTRALIZATION.md).
It checks, one by one, the networking pieces the decentralized design depends on.
Status, 2026-10-08: **the one-machine part is done. The two-machine test (laptop + cloud pod) is next.**

## The short version

- **On one machine, the basic path works.** A worker behind a relay advertised "I serve
  model X" in the DHT. A requester found it, reached it **through the relay**, sent a
  message and got a reply. In private mode neither end ever learned the other's address.
- **Hole punching (DCUtR) and NAT detection (AutoNAT) can't be judged on one machine.**
  AutoNAT is also **broken in py-libp2p 0.8.0**: it reports "publicly reachable" even for
  a closed port.
- **Windows is fine.** py-libp2p 0.8.0 installs natively on Windows with Python 3.12 or
  3.13. It does not install on 3.14 yet. The old fastecdsa problem is gone: Windows skips
  that package.
- **For tomorrow I recommend option (a): your laptop is the relay.** You need to decide
  whether it's OK to open one TCP port (4001) on your home router for about 30 minutes,
  either automatically (UPnP) or by hand (port forward).

## What's in this folder

| File | What it is |
| --- | --- |
| `p2p_spike.py` | The spike. Roles: `relay`, `worker`, `requester`. You can join them with `+` to run several in one process. |
| `run_local.sh` | The local test. It starts the relay, 2 workers and 2 requesters as separate processes on 127.0.0.1, checks the results, and stops them all. |
| `run_laptop.ps1` / `run_laptop.sh` | The one command for the laptop tomorrow (Windows / macOS-Linux). |
| `install.ps1` / `install.sh` | These create `.venv/` here and install py-libp2p. The run scripts call them on first use. |
| `probes.py` | Small one-machine tests behind the findings below (`dht`, `autonat`, `dcutr`). |
| `requirements.txt` | `libp2p==0.8.0` |

## What the local run proved

`./run_local.sh` runs every process on 127.0.0.1 and passes 10 of 10 checks. It passed
5 runs in a row. Results for each piece:

| Piece | Result on one machine | Notes |
| --- | --- | --- |
| Relay slot reservation (Circuit Relay v2) | **Works** | The library's own example never reserves in time: its relay search runs before it connects, then sleeps 5 minutes, so the dialer gets error 204 "no reservation". The kit reserves explicitly. |
| Connection through the relay | **Works** | The relayed connection is encrypted end to end. The relay passes bytes it cannot read. |
| Message + reply over the relay | **Works** | |
| DHT: "worker serves model X" record + lookup | **Works** | A record with no address is silently dropped by the DHT, even though the publish call still says it succeeded. |
| DHT: two peers that only know the seed find a third | **Works** (`probes.py dht`) | Value records (put/get) also work. Lookups sometimes return the same peer twice. |
| DCUtR hole punch to a direct connection | **Can't be tested on one machine** | On one machine every address is reachable, so the "upgrade" is just a plain direct dial. The DCUtR message exchange itself does run between two py-libp2p peers and produced a direct connection (`probes.py dcutr`). But the library reported failure while that connection existed, and opened it twice, so the kit checks the actual connections instead of trusting the return value. A real test needs **three networks** (see below). |
| AutoNAT reachability detection | **Broken in 0.8.0** (`probes.py autonat`) | Four servers were asked to dial back a **closed** port. All four said "OK" and the status became PUBLIC. The cause: the server's dial-back reuses the connection the request came in on, so it always succeeds. Every node would believe it is reachable. Don't use it to decide who can be a relay. |
| UPnP automatic port mapping | **Exists; nothing to map here** | The library finds the router, maps the same port number, and refuses when the router sits behind another NAT (double NAT). This cloud pod has no router: result `UNAVAILABLE`. The library's message on the pod ("devices were found, but none are valid gateways") is misleading; really none were found. Tomorrow is the real test. The kit removes its mapping on exit. Otherwise it would stay until the router reboots. |

Running the bundled examples (`examples/nat/{relay,listener,dialer}.py`) on 127.0.0.1
confirmed the same things. The example's "connection type" label is also unreliable. It
printed "RELAYED" for a message that actually went over a direct connection.

### Who sees whose IP (Rule 1 / Rule 6)

| Moment | Who learns an IP |
| --- | --- |
| A peer connects to its relay | The relay sees that peer's IP and port. This can't be avoided. |
| A worker publishes its DHT record | **Direct mode:** the record contains the worker's own listening addresses, which on a home machine means its LAN IP. Anyone who looks it up sees them. **Private mode:** the record contains only `<relay>/p2p-circuit/p2p/<worker>`. |
| Normal DHT traffic (publishing or looking up a record) | **Every DHT peer you talk to sees your IP**, because the DHT dials peers directly. The local run caught this. A "private" worker, while storing its record, connected straight to the other worker. Private mode in the kit now refuses every direct connection except to its relay, and uses the DHT only through the relay. |
| Request through the relay | Worker and requester each see only the relay. Confirmed: in the private run, the worker's only direct connection was to the relay, and the same for the requester. |
| After a hole-punch upgrade (direct mode) | Worker and requester see each other's IP and port. Confirmed on 127.0.0.1. |

From the private-mode run:

```
[requester] private mode: refused a direct dial to 16Uiu2HA..TgjpUo (it would have seen our IP)
[requester] RESULT dht_lookup OK found 16Uiu2HA..TgjpUo
[requester] SEES peer 16Uiu2HA..TgjpUo only via the relay (its IP is not visible here)
[requester] RESULT relayed_connection OK RELAYED (socket goes to the relay, 127.0.0.1:47121)
[requester] RESULT message OK over RELAYED connection
[requester]   the worker saw me as: RELAYED (socket goes to the relay, 127.0.0.1:47121)
```

**What this means for the design:** "Private mode" has to cover DHT traffic too, not just
the request. A worker behind NAT should not publish its LAN addresses. The relay sees both
IPs plus the timing and size of the traffic, but not the content.

### Library problems found (py-libp2p 0.8.0, possibly worth reporting upstream)

1. AutoNAT dial-back says "reachable" for everyone (described above).
2. A relay address can't be advertised through the normal API. The library cuts it at the
   first `/p2p/`, which turns it into the relay's IP with the worker's ID. The kit works
   around this by building the list of advertised addresses itself.
3. The DHT silently drops records that have no address, while `provide()` still reports success.
4. The relay search runs before the peer connects and then waits 5 minutes. It can also
   decide too early that a peer is not a relay, before identification finishes. The kit
   reserves explicitly.
5. `initiate_hole_punch()` reports failure even when a direct connection was made, and
   opens that connection twice.

## The laptop question

Your laptop is behind your home router. This pod is behind RunPod's network and can't
accept incoming connections. **Two machines that are both behind NAT can't find each other
unless at least one reachable machine helps.** So one of these has to happen:

### (a) The laptop is the reachable relay (recommended)

- **What it needs:**
  - UPnP turned on in your router. The script tries it automatically and prints `RESULT upnp MAPPED` if it works.
  - If UPnP doesn't work: a manual port forward in your router's admin page: **TCP 4001 → the LAN address the script prints**.
  - Windows asks whether Python may accept incoming connections. Allow it on **Private networks**, and make sure your home Wi-Fi is set to Private.
  - The laptop stays awake during the test.
- **One check first:** your internet connection must not be CGNAT. Compare the WAN/Internet
  IP shown on your router's status page with what a "what is my IP" site shows. If they
  differ, or the router's address starts with 100.64–100.127, 10., 172.16–31. or 192.168.,
  your ISP shares one public IP among customers. Then (a) can't work, so go to (c).
- **What it exposes:** one TCP port on your home IP, served by a beta py-libp2p process,
  for the duration of the test. The relay limits itself to 16 circuits of at most 100 MB
  and 1 hour each. Ctrl+C removes a UPnP mapping automatically. Delete a manual forward
  yourself afterwards.
- **Cost:** none. No change to the pod; it only makes outgoing connections.
- **What it tests:**
  - a connection across real networks where the pod's worker is reachable *only* through your laptop
  - the DHT across machines
  - UPnP on a real router
  - who sees which real IP

  It **doesn't** test hole punching, because the relay is on the same machine as one end.

### (b) The pod exposes one TCP port through RunPod

- **What it needs:** editing the pod in RunPod to add a TCP port. **RunPod's documentation
  says editing a running pod "resets it completely, erasing all data not stored in
  /workspace or a network volume."** On this pod the working folders live outside
  /workspace, so the reset would erase them, and it would stop work that is running now.
  The external port number is assigned by RunPod and differs from the internal one. RunPod
  pods support TCP only (no UDP), which matters later for QUIC.
- **What it exposes:** that port on the pod, publicly.
- **Your call.** I don't recommend it unless the data outside /workspace is moved or backed up first.

### (c) A third reachable machine is the relay

Options: a small VPS (a few dollars a month, billed by the hour at most providers), a
friend's machine with a port forward, or a small CPU pod with an exposed TCP port (billed
hourly, so only if you approve it). Run `./install.sh` there, then
`.venv/bin/python p2p_spike.py relay --host 0.0.0.0 --port 4001`. The laptop and the pod
each run `worker+requester` against it.

- **This is the only option that can test hole punching**, because the relay and the two
  peers are on three different networks. It's also closest to the real design, where
  volunteers run relays.
- **Cost:** money and about 20 minutes of setup.
- The Shadow PC is a cloud PC, very likely also behind NAT with no incoming ports, so it is
  probably not a candidate.

**Recommendation:** do (a) tomorrow. It's free, it doesn't touch the pod, and it answers
the doc's open UPnP question on real hardware. If your connection is CGNAT, or once (a)
works, do (c) to test hole punching. Avoid (b) unless the reset risk is handled.

**What you decide:**
1. Is it OK to open TCP 4001 on your home network for the test?
2. If not, or if you're on CGNAT: is (c) OK, and with which machine?

### Steps for tomorrow: Windows laptop

Once, if not installed: `winget install Git.Git Python.Python.3.12`, then open a new PowerShell.

```powershell
git clone -b p2p-spike https://github.com/quacktheplanet/deai.git deai-p2p
cd deai-p2p
powershell -ExecutionPolicy Bypass -File spike\p2p\run_laptop.ps1
```

That one command installs everything on the first run, then starts the relay, a worker
(`laptop-echo`) and a requester (which waits up to 15 minutes for the pod's `pod-echo`).

1. Look for `RESULT upnp MAPPED`. If it says `UNAVAILABLE` or `MAP_FAILED`, add the port
   forward (TCP 4001 → the LAN address it printed), press Ctrl+C, and run the command again.
2. Send Claude two things: your public IP (shown in the `UPnP: gateway found, external IP`
   line, or from a "what is my IP" site), and the line `[relay] peer id 16Uiu2...`.
3. Claude starts the pod side. It makes only outgoing connections and listens on 127.0.0.1:
   `.venv/bin/python -u p2p_spike.py worker+requester --relay /ip4/<IP>/tcp/4001/p2p/<ID> --model pod-echo --want laptop-echo --wait 300 --lifetime 900`
4. Success looks like `[requester] RESULT message OK over RELAYED connection` and
   `SUMMARY SUCCESS` on both machines. `dcutr_upgrade STAYED_RELAYED` is expected here.
   The relay's `SEES` lines show exactly which IP each machine saw.
5. Ctrl+C when done (it also stops by itself after 30 minutes). Remove the manual port
   forward if you added one. The output is saved in `spike\p2p\laptop-run.log`.

### Steps for tomorrow: macOS or Linux laptop

```bash
git clone -b p2p-spike https://github.com/quacktheplanet/deai.git deai-p2p
cd deai-p2p
./spike/p2p/run_laptop.sh
```

The rest is the same as on Windows.
- **macOS:** Python 3.12 (`brew install python@3.12`) on Apple Silicon installs from ready-made packages. Any other combination needs `brew install gmp`, which `install.sh` handles. Allow the firewall prompt.
- **Linux:** see the gotcha below.

## Install notes per platform

Checked against PyPI by resolving the dependencies for each platform and checking that
every compiled package has a ready-made wheel for it:

| Platform | Result |
| --- | --- |
| Windows, Python 3.12 / 3.13 | **All wheels, no compiler needed.** fastecdsa is excluded on Windows, so libp2p uses coincurve instead. py-libp2p's CI runs its tests on Windows with Python 3.11–3.13. *Not yet run on a real Windows machine by us.* |
| Windows, Python 3.14 | No: coincurve 21.0.0 and miniupnpc have no 3.14 wheels, so they'd need a C compiler. Use 3.12. |
| macOS Apple Silicon, Python 3.12 | All wheels (fastecdsa's wheel needs macOS 14 or later). |
| macOS with Python 3.13, or Intel Macs | fastecdsa compiles from source: needs `brew install gmp`. |
| Linux, any Python | fastecdsa 2.3.2 has **no Linux wheels**, so it always compiles. You need GMP headers, a C compiler and Python headers (`sudo apt install libgmp-dev build-essential python3-dev python3-venv`). |

**Linux install gotcha (no sudo, as on this pod):**
1. Download the package without installing it: `apt-get download libgmp-dev`.
2. Unpack it: `dpkg-deb -x libgmp-dev_*.deb $HOME/gmpdev`.
3. Point the compiler at it and install:
   `CFLAGS="-I$HOME/gmpdev/usr/include -I$HOME/gmpdev/usr/include/x86_64-linux-gnu" LDFLAGS="-L$HOME/gmpdev/usr/lib/x86_64-linux-gnu" ./install.sh`

The runtime library `libgmp.so.10` is usually already installed.

**WSL2** is a fallback only if the native Windows install fails. With WSL2's default
networking, the Linux VM sits behind another NAT. To act as a relay it then needs
`networkingMode=mirrored` in `%UserProfile%\.wslconfig` (Windows 11), or a
`netsh interface portproxy` rule. **Hivemind**, the doc's fallback library, runs on Linux
(macOS partly) and would need WSL2 on Windows.

## Running it yourself

```bash
./install.sh                      # or install.ps1 on Windows
./run_local.sh                    # local test; prints PASS/FAIL per check
python p2p_spike.py --help        # all options; --verbose shows libp2p's own logs
.venv/bin/python probes.py autonat   # or dht / dcutr
```

`--privacy private` keeps a peer relay-only: it never upgrades to a direct connection,
publishes no address of its own, and makes no direct connection except to its relay.
`--privacy direct` (the default) allows the hole-punch upgrade.
