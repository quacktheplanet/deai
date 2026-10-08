#!/usr/bin/env python3
"""
DAI peer-to-peer networking spike (py-libp2p 0.8.0).

Roles (combine several in one process with "+", e.g. relay+worker+requester):

  relay      A reachable peer. Relays traffic for peers behind NAT (Circuit Relay v2),
             is the DHT seed (Kademlia server) and answers AutoNAT dial-back checks.
  worker     A peer behind NAT. Dials OUT to the relay, reserves a relay slot and
             advertises "I serve model X" in the DHT. Answers requests.
  requester  A peer behind NAT. Dials OUT to the relay, looks model X up in the DHT,
             reaches the worker THROUGH the relay, tries to upgrade to a direct
             connection (DCUtR hole punch), sends a message and prints the reply.

Every step prints what happened and which addresses each side observed, because the
design question is "who sees whose IP" (docs/DECENTRALIZATION.md, Rules 1 and 6).
Lines starting with "RESULT" are the outcome of one check.

Everything listens on 127.0.0.1 by default. --host 0.0.0.0 (relay only) is for a
machine you deliberately make reachable from the internet.

Local example (three terminals, or see run_local.sh):
  python p2p_spike.py relay --port 4001
  python p2p_spike.py worker    --relay /ip4/127.0.0.1/tcp/4001/p2p/<RELAY_ID> --model llama-3.1-8b
  python p2p_spike.py requester --relay /ip4/127.0.0.1/tcp/4001/p2p/<RELAY_ID> --want llama-3.1-8b
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from contextlib import AsyncExitStack

import multiaddr
import trio

from libp2p import new_host
from libp2p.abc import INetConn, INetStream, INetwork, INotifee
from libp2p.connection_types import ConnectionType
from libp2p.crypto.secp256k1 import create_new_key_pair
from libp2p.custom_types import TProtocol
from libp2p.host.autonat import AutoNATService
from libp2p.kad_dht.kad_dht import DHTMode, KadDHT
from libp2p.peer.id import ID
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.relay.circuit_v2.config import RelayConfig, RelayRole
from libp2p.relay.circuit_v2.dcutr import DCUtRProtocol
from libp2p.relay.circuit_v2.discovery import RelayDiscovery
from libp2p.relay.circuit_v2.protocol import CircuitV2Protocol
from libp2p.relay.circuit_v2.resources import RelayLimits
from libp2p.relay.circuit_v2.transport import CircuitV2Transport
from libp2p.tools.anyio_service import background_trio_service

APP_PROTOCOL = TProtocol("/dai-spike/1.0.0")
MAX_MSG = 64 * 1024


# --------------------------------------------------------------------------- output


class Out:
    """Prefixes every line with the role, so combined roles stay readable."""

    def __init__(self, role: str) -> None:
        self.role = role

    def say(self, msg: str) -> None:
        print(f"[{self.role}] {msg}", flush=True)

    def result(self, name: str, status: str, detail: str = "") -> None:
        print(f"[{self.role}] RESULT {name} {status} {detail}".rstrip(), flush=True)


def short(peer_id: ID | str) -> str:
    s = str(peer_id)
    return f"{s[:8]}..{s[-6:]}"


def model_key(model: str) -> str:
    return f"dai-model:{model}"


def conn_kind(conn: INetConn) -> str:
    try:
        return "RELAYED" if conn.get_connection_type() == ConnectionType.RELAYED else "DIRECT"
    except Exception:
        return "UNKNOWN"


def conn_remote(conn: INetConn) -> str:
    """The socket-level address of the other end, as this side sees it."""
    try:
        r = conn.muxed_conn.get_remote_address()  # type: ignore[attr-defined]
        return f"{r[0]}:{r[1]}" if r else "?"
    except Exception:
        return "?"


def conns_to(host, peer_id: ID) -> list[INetConn]:
    c = host.get_network().connections.get(peer_id) or []
    return c if isinstance(c, list) else [c]


def describe_conns(host, peer_id: ID) -> list[str]:
    out = []
    for c in conns_to(host, peer_id):
        if conn_kind(c) == "RELAYED":
            out.append(f"RELAYED (socket goes to the relay, {conn_remote(c)})")
        else:
            out.append(f"DIRECT (socket {conn_remote(c)})")
    return out


class ConnLogger(INotifee):
    """Prints every new connection: this is 'who sees whose address'."""

    def __init__(self, out: Out) -> None:
        self.out = out

    async def connected(self, network: INetwork, conn: INetConn) -> None:
        peer = conn.muxed_conn.peer_id
        if conn_kind(conn) == "RELAYED":
            self.out.say(f"SEES peer {short(peer)} only via the relay (its IP is not visible here)")
        else:
            self.out.say(f"SEES peer {short(peer)} at {conn_remote(conn)} (direct socket)")

    async def disconnected(self, network: INetwork, conn: INetConn) -> None:
        return None

    async def opened_stream(self, network, stream) -> None:
        return None

    async def closed_stream(self, network, stream) -> None:
        return None

    async def listen(self, network, maddr) -> None:
        return None

    async def listen_close(self, network, maddr) -> None:
        return None


def make_host(out: Out):
    host = new_host(key_pair=create_new_key_pair())
    host.get_network().register_notifee(ConnLogger(out))
    return host


def make_relay_stack(host, *, hop: bool):
    """Circuit Relay v2 protocol + transport. hop=True makes this peer a relay."""
    limits = RelayLimits(
        duration=3600, data=100 * 1024 * 1024, max_circuit_conns=16, max_reservations=16
    )
    roles = RelayRole.STOP | RelayRole.CLIENT | (RelayRole.HOP if hop else RelayRole(0))
    protocol = CircuitV2Protocol(host, limits=limits, allow_hop=hop)
    transport = CircuitV2Transport(host, protocol, RelayConfig(roles=roles, limits=limits))
    return protocol, transport


async def connect_to_relay(out: Out, host, relay_addr: str):
    info = info_from_p2p_addr(multiaddr.Multiaddr(relay_addr))
    out.say(f"dialing relay {short(info.peer_id)} at {relay_addr.split('/p2p/')[0]} ...")
    with trio.fail_after(20):
        await host.connect(info)
    return info


def only_dial_relay(out: Out, host, relay_id: ID) -> None:
    """Private mode: refuse to open a direct socket to anyone except our relay.

    Without this, ordinary DHT work (storing or looking up a record) dials other DHT
    peers directly, and each of them then sees our IP. The local run showed exactly
    that. With it, all our traffic goes to the relay, and the DHT is used only through
    the relay (it is our DHT server).
    """
    from libp2p.network.exceptions import SwarmException

    net = host.get_network()
    original = net.dial_peer
    refused: set[ID] = set()

    async def dial_peer(peer_id: ID, *a, **kw):
        if peer_id != relay_id:
            if peer_id not in refused:
                refused.add(peer_id)
                out.say(f"private mode: refused a direct dial to {short(peer_id)} "
                        "(it would have seen our IP)")
            raise SwarmException("private mode: direct dials to non-relay peers are disabled")
        return await original(peer_id, *a, **kw)

    net.dial_peer = dial_peer  # type: ignore[method-assign]


async def sleep_or_forever(seconds: float) -> None:
    await (trio.sleep(seconds) if seconds > 0 else trio.sleep_forever())


# --------------------------------------------------------------------------- relay


async def run_relay(args, ready: trio.Event) -> None:
    out = Out("relay")
    host = make_host(out)
    protocol, _ = make_relay_stack(host, hop=True)
    AutoNATService(host)  # registers its handler; answers dial-back requests
    dht = KadDHT(host, DHTMode.SERVER)
    upnp = None

    listen = multiaddr.Multiaddr(f"/ip4/{args.host}/tcp/{args.port}")
    async with host.run(listen_addrs=[listen]):
        async with background_trio_service(protocol), background_trio_service(dht):
            try:
                out.say(f"peer id {host.get_id()}")
                out.say(f"listening on {args.host}:{args.port}")
                out.say("roles: circuit relay v2 (hop) + DHT seed (server) + AutoNAT server")
                if args.upnp:
                    upnp = await try_upnp(out, args.port, map_port=args.host != "127.0.0.1")
                local = "127.0.0.1" if args.host == "0.0.0.0" else args.host
                args.relay_local = f"/ip4/{local}/tcp/{args.port}/p2p/{host.get_id()}"
                print(f"RELAY_ADDR {args.relay_local}", flush=True)
                if args.host != "127.0.0.1":
                    out.say("Other machines dial:  /ip4/<THIS-NETWORK'S-PUBLIC-IP>/tcp/"
                            f"{args.port}/p2p/{host.get_id()}")
                ready.set()
                out.say("ready")
                await report_observed_addrs(out, host)
            finally:
                if upnp is not None:
                    with trio.move_on_after(5, shield=True):
                        await upnp.remove_port_mapping(args.port, "TCP")
                        out.say(f"UPnP: removed the mapping for port {args.port}")


async def report_observed_addrs(out: Out, host) -> None:
    """Print how other peers see this relay (identify 'observed address')."""
    seen: set[str] = set()
    mgr = getattr(host, "_observed_addr_manager", None)
    while True:
        await trio.sleep(5)
        if mgr is None:
            continue
        for a in mgr.addrs(min_observers=1):
            if str(a) not in seen:
                seen.add(str(a))
                out.say(f"other peers see this relay at {a}")


async def try_upnp(out: Out, port: int, map_port: bool):
    """Report what UPnP finds. Requests a mapping only for a non-loopback relay."""
    from libp2p.discovery.upnp.upnp import UpnpManager

    mgr = UpnpManager()
    out.say("UPnP: looking for an Internet Gateway Device on the local network ...")
    if not await mgr.discover():
        out.result("upnp", "UNAVAILABLE",
                   "no usable UPnP gateway (none found, UPnP disabled, or double NAT)")
        return None
    out.say(f"UPnP: gateway found, external IP {mgr.get_external_ip()}")
    if not map_port:
        out.result("upnp", "GATEWAY_FOUND", "mapping not requested (needs --host 0.0.0.0)")
        return None
    if await mgr.add_port_mapping(port, "TCP"):
        out.result("upnp", "MAPPED",
                   f"router forwards {mgr.get_external_ip()}:{port} to this machine")
        return mgr
    out.result("upnp", "MAP_FAILED", "router refused the mapping")
    return None


# --------------------------------------------------------------------------- worker


async def run_worker(args, relay_addr: str) -> None:
    out = Out("worker")
    relay_info = info_from_p2p_addr(multiaddr.Multiaddr(relay_addr))
    host = make_host(out)

    # What this worker advertises (identify + DHT provider record):
    #   private: ONLY "<relay>/p2p-circuit/p2p/<me>" -> nobody learns our IP from records.
    #   direct:  our listen addresses + the circuit address.
    # Workaround: py-libp2p 0.8.0's get_addrs() cuts every address at its first /p2p/
    # and appends ours, so a circuit address given via announce_addrs/addrs_factory
    # comes out as "<relay ip:port>/p2p/<me>", a wrong address. We build the list
    # ourselves. (The DHT also drops provider records that carry no address at all.)
    listen_only = host.get_addrs
    my_circuit = multiaddr.Multiaddr(f"{relay_addr}/p2p-circuit/p2p/{host.get_id()}")

    def advertised_addrs():
        return [my_circuit] if args.privacy == "private" else listen_only() + [my_circuit]

    host.get_addrs = advertised_addrs  # type: ignore[method-assign]
    if args.privacy == "private":
        only_dial_relay(out, host, relay_info.peer_id)

    protocol, transport = make_relay_stack(host, hop=False)
    discovery = RelayDiscovery(host, auto_reserve=True)
    transport.discovery = discovery
    dht = KadDHT(host, DHTMode.CLIENT)

    async def handle(stream: INetStream) -> None:
        peer = stream.muxed_conn.peer_id
        try:
            req = json.loads((await stream.read(MAX_MSG)).decode())
            seen = describe_conns(host, peer)
            out.say(f"request from {short(peer)}: {req.get('prompt')!r}")
            for line in seen:
                out.say(f"  I see the requester as: {line}")
            reply = {
                "worker": str(host.get_id()),
                "model": args.model,
                "answer": f"echo from {args.model}: {req.get('prompt')}",
                "worker_sees_requester_as": seen,
            }
            await stream.write(json.dumps(reply).encode())
        except Exception as e:
            out.say(f"error handling request: {e!r}")
        finally:
            await stream.close()

    listen = multiaddr.Multiaddr(f"/ip4/{args.peer_host}/tcp/0")
    async with host.run(listen_addrs=[listen]), AsyncExitStack() as stack:
        host.set_stream_handler(APP_PROTOCOL, handle)
        for svc in (protocol, dht, discovery):
            await stack.enter_async_context(background_trio_service(svc))
        if args.privacy == "direct":
            # DCUtR answers (and itself drives) hole punches. In private mode it is
            # never started, so this worker never upgrades to a direct connection.
            await stack.enter_async_context(background_trio_service(DCUtRProtocol(host)))

        out.say(f"peer id {host.get_id()}  model={args.model}  privacy={args.privacy}")
        await connect_to_relay(out, host, relay_addr)

        # Reserve a relay slot, explicitly. Left alone, RelayDiscovery's first pass runs
        # at startup (before we are connected) and the next one 5 minutes later; and its
        # discover_relays() can cache "not a relay" if it runs before identify finishes.
        ri = None
        for _attempt in range(3):
            await discovery._add_relay(relay_info.peer_id)  # adds + reserves (auto_reserve)
            ri = discovery.get_relay_info(relay_info.peer_id)
            if ri and ri.has_reservation:
                break
            await trio.sleep(2)
        if not (ri and ri.has_reservation):
            out.result("reservation", "FAILED", "relay refused or did not answer")
            return
        out.result("reservation", "OK", f"reachable as {my_circuit}")

        await dht.routing_table.add_peer(relay_info.peer_id)
        ok = await dht.provide(model_key(args.model))
        out.result("dht_provide", "OK" if ok else "FAILED", f"key={model_key(args.model)}")
        out.say(f"the DHT record will publish: {[str(a) for a in host.get_addrs()]}")
        out.say("ready, waiting for requests")
        await trio.sleep_forever()


# --------------------------------------------------------------------------- requester


async def run_requester(args, relay_addr: str) -> bool:
    out = Out("requester")
    relay_info = info_from_p2p_addr(multiaddr.Multiaddr(relay_addr))
    host = make_host(out)
    protocol, transport = make_relay_stack(host, hop=False)
    dht = KadDHT(host, DHTMode.CLIENT)
    dcutr = DCUtRProtocol(host)
    autonat = AutoNATService(host, serve=False)
    if args.privacy == "private":
        only_dial_relay(out, host, relay_info.peer_id)

    listen = multiaddr.Multiaddr(f"/ip4/{args.peer_host}/tcp/0")
    async with host.run(listen_addrs=[listen]), AsyncExitStack() as stack:
        for svc in (protocol, dht) + ((dcutr,) if args.privacy == "direct" else ()):
            await stack.enter_async_context(background_trio_service(svc))
        out.say(f"peer id {host.get_id()}  privacy={args.privacy}")
        await connect_to_relay(out, host, relay_addr)
        await dht.routing_table.add_peer(relay_info.peer_id)

        # 1. Discovery: who serves this model?
        key = model_key(args.want)
        out.say(f"looking up {key!r} in the DHT (up to {args.wait:.0f}s) ...")
        providers = []
        deadline = time.monotonic() + args.wait
        while not providers and time.monotonic() < deadline:
            providers = [p for p in await dht.find_providers(key) if p.peer_id != host.get_id()]
            if not providers:
                await trio.sleep(3)
        if not providers:
            out.result("dht_lookup", "FAILED", f"no provider for {key}")
            return False
        worker = providers[0]
        out.result("dht_lookup", "OK", f"found {short(worker.peer_id)}")
        out.say(f"the DHT record publishes for it: {sorted({str(a) for a in worker.addrs})}")

        # 2. Reach the worker THROUGH the relay named in its record.
        circuits = [a for a in worker.addrs if "/p2p-circuit" in str(a)]
        circuit = circuits[0] if circuits else multiaddr.Multiaddr(
            f"{relay_addr}/p2p-circuit/p2p/{worker.peer_id}")
        if describe_conns(host, worker.peer_id):
            out.say(f"NOTE: already connected directly before the relay step "
                    f"{describe_conns(host, worker.peer_id)}: the DHT lookup dialed the "
                    "worker's advertised address. Across real NATs that dial fails.")
        out.say(f"dialing worker through relay: {circuit}")
        try:
            with trio.fail_after(20):
                await transport.dial(circuit)
        except Exception as e:
            out.result("relayed_connection", "FAILED", repr(e))
            return False
        relayed = [c for c in describe_conns(host, worker.peer_id) if c.startswith("RELAYED")]
        if not relayed:
            out.result("relayed_connection", "FAILED", "no relayed connection after dial")
            return False
        out.result("relayed_connection", "OK", relayed[0])

        # 3. Try to upgrade to direct (DCUtR) unless in private mode.
        if args.privacy == "private":
            out.result("dcutr_upgrade", "SKIPPED", "private mode: stay relayed, IPs stay hidden")
        else:
            out.say("trying DCUtR upgrade to a direct connection ...")
            returned = None
            with trio.move_on_after(30):
                returned = await dcutr.initiate_hole_punch(worker.peer_id)
            await trio.sleep(1)  # the other side's dial may land just after
            now = describe_conns(host, worker.peer_id)
            direct = any(c.startswith("DIRECT") for c in now)
            # initiate_hole_punch() only counts its own dials, so check connections.
            out.result("dcutr_upgrade", "DIRECT" if direct else "STAYED_RELAYED",
                       f"(library returned {returned}) connections now: {now}")

        # 4. Send the request, read the reply.
        stream = await host.new_stream(worker.peer_id, [APP_PROTOCOL])
        used = next((conn_kind(c) for c in conns_to(host, worker.peer_id)
                     if c.muxed_conn is stream.muxed_conn), "UNKNOWN")
        await stream.write(json.dumps({"model": args.want, "prompt": args.prompt}).encode())
        with trio.fail_after(30):
            reply = json.loads((await stream.read(MAX_MSG)).decode())
        await stream.close()
        out.result("message", "OK", f"over {used} connection")
        out.say(f"reply: {reply['answer']!r}")
        for line in reply.get("worker_sees_requester_as", []):
            out.say(f"  the worker saw me as: {line}")

        # 5. AutoNAT: ask the relay to dial us back.
        try:
            with trio.fail_after(15):
                status, _ = await autonat.query_server(relay_info.peer_id)
            out.result("autonat_query", "ANSWERED",
                       f"relay verdict={'OK (reachable)' if status == 0 else status}; "
                       "NOTE py-libp2p 0.8.0 answers OK even for unreachable peers "
                       "(see README), so this is not evidence of reachability")
        except Exception as e:
            out.result("autonat_query", "FAILED", repr(e))
        return True


# --------------------------------------------------------------------------- main


async def run(args) -> int:
    roles = args.role.split("+")
    ok = True
    async with trio.open_nursery() as nursery:
        relay_addr = args.relay
        if "relay" in roles:
            ready = trio.Event()
            nursery.start_soon(run_relay, args, ready)
            await ready.wait()
            relay_addr = relay_addr or args.relay_local
        if "worker" in roles:
            nursery.start_soon(run_worker, args, relay_addr)
            await trio.sleep(3)  # let it reserve + advertise before a local requester looks
        if "requester" in roles:
            ok = await run_requester(args, relay_addr)
            Out("requester").say("SUMMARY " + ("SUCCESS" if ok else "FAILED"))
            if roles == ["requester"]:
                nursery.cancel_scope.cancel()
                return 0 if ok else 1
        if args.lifetime > 0:
            Out(args.role).say(f"staying up for {args.lifetime:.0f}s (Ctrl+C to stop)")
        else:
            Out(args.role).say("staying up (Ctrl+C to stop)")
        await sleep_or_forever(args.lifetime)
        nursery.cancel_scope.cancel()
    return 0 if ok else 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("role", help="relay | worker | requester, or several joined with '+'")
    p.add_argument("--relay", help="relay multiaddr: /ip4/<ip>/tcp/<port>/p2p/<relay peer id>")
    p.add_argument("--host", default="127.0.0.1",
                   help="relay listen interface (default 127.0.0.1; 0.0.0.0 only on a "
                        "machine you mean to make reachable)")
    p.add_argument("--port", type=int, default=4001, help="relay TCP port (default 4001)")
    p.add_argument("--peer-host", default="127.0.0.1",
                   help="worker/requester listen interface (default 127.0.0.1)")
    p.add_argument("--model", default="llama-3.1-8b", help="worker: model name it serves")
    p.add_argument("--want", help="requester: model name to look up (default: --model)")
    p.add_argument("--prompt", default="hello from the requester")
    p.add_argument("--privacy", choices=["direct", "private"], default="direct",
                   help="direct: allow the hole-punch upgrade (both ends then learn each "
                        "other's IP). private: relay only, never upgrade.")
    p.add_argument("--upnp", action="store_true",
                   help="relay: look for a UPnP router; with --host 0.0.0.0 also map the port")
    p.add_argument("--wait", type=float, default=60, help="requester: seconds to wait for a provider")
    p.add_argument("--lifetime", type=float, default=0,
                   help="exit after N seconds (0 = run until Ctrl+C)")
    p.add_argument("--verbose", action="store_true", help="show libp2p's own logs")
    args = p.parse_args()

    roles = args.role.split("+")
    if not roles or any(r not in ("relay", "worker", "requester") for r in roles):
        p.error("role must be relay, worker, requester, or a '+' combination")
    if "relay" not in roles and not args.relay:
        p.error("--relay is required unless this process also runs the relay")
    args.want = args.want or args.model

    level = logging.DEBUG if args.verbose else logging.CRITICAL
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("libp2p").setLevel(level)  # libp2p logs a lot of expected noise

    try:
        code = trio.run(run, args)
    except BaseException as e:  # Ctrl+C may arrive wrapped in an exception group
        if not is_interrupt(e):
            raise
        print("stopped", flush=True)
        code = 130
    sys.exit(code)


def is_interrupt(e: BaseException) -> bool:
    if isinstance(e, KeyboardInterrupt):
        return True
    inner = getattr(e, "exceptions", None)
    return bool(inner) and all(is_interrupt(x) for x in inner)


if __name__ == "__main__":
    main()
