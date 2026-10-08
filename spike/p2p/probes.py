#!/usr/bin/env python3
"""
Single-host probes behind the findings in README.md. Everything on 127.0.0.1.

  python probes.py dht      two peers that know only a seed find a third peer (FIND_NODE),
                            plus a value record and a provider record round trip
  python probes.py autonat  asks 4 AutoNAT servers to dial us back at a CLOSED port;
                            the correct verdict is PRIVATE
  python probes.py dcutr    worker hides its addresses, requester reaches it via a relay,
                            then runs the DCUtR CONNECT/SYNC exchange
Optional: --base-port N (default 47150; uses N..N+40).
"""

import argparse
import logging
from contextlib import AsyncExitStack

import multiaddr
import trio

from libp2p import new_host
from libp2p.crypto.secp256k1 import create_new_key_pair
from libp2p.host.autonat import AutoNATService
from libp2p.kad_dht.kad_dht import DHTMode, KadDHT
from libp2p.peer.peerinfo import PeerInfo
from libp2p.relay.circuit_v2.dcutr import DCUtRProtocol
from libp2p.relay.circuit_v2.discovery import RelayDiscovery
from libp2p.tools.anyio_service import background_trio_service

import p2p_spike as kit


def la(port: int):
    return [multiaddr.Multiaddr(f"/ip4/127.0.0.1/tcp/{port}")]


def host():
    return new_host(key_pair=create_new_key_pair())


async def probe_dht(base: int) -> None:
    for c_mode in (DHTMode.SERVER, DHTMode.CLIENT):
        S, A, B, C = hosts = [host() for _ in range(4)]
        async with AsyncExitStack() as st:
            for i, h in enumerate(hosts):
                await st.enter_async_context(h.run(listen_addrs=la(base + i)))
            dhts = [KadDHT(S, DHTMode.SERVER), KadDHT(A, DHTMode.SERVER),
                    KadDHT(B, DHTMode.SERVER), KadDHT(C, c_mode)]
            for d in dhts:
                await st.enter_async_context(background_trio_service(d))
            seed = PeerInfo(S.get_id(), S.get_addrs())
            for h, d in zip(hosts[1:], dhts[1:]):  # everyone knows only the seed
                await h.connect(seed)
                await d.routing_table.add_peer(S.get_id())
            await trio.sleep(3)
            for name, h, d in (("A", A, dhts[1]), ("B", B, dhts[2])):
                knew = C.get_id() in h.get_peerstore().peer_ids()
                info = None
                with trio.move_on_after(15):
                    info = await d.find_peer(C.get_id())
                print(f"PROBE dht_find_peer C={c_mode.name} finder={name} knew_C_before={knew} "
                      f"found={bool(info)}")
            await dhts[3].put_value("dai-model-index:qwen-7b", b'{"peer":"C"}')
            v = None
            with trio.move_on_after(10):
                v = await dhts[1].get_value("dai-model-index:qwen-7b")
            print(f"PROBE dht_value C={c_mode.name} A_reads_C's_record={v!r}")
            await dhts[3].provide("dai-model:qwen-7b")
            provs = []
            with trio.move_on_after(10):
                provs = await dhts[2].find_providers("dai-model:qwen-7b")
            ids = [str(p.peer_id) == str(C.get_id()) for p in provs]
            print(f"PROBE dht_provider C={c_mode.name} B_finds_C={any(ids)} "
                  f"(entries returned: {len(ids)})")


async def probe_autonat(base: int) -> None:
    servers = [host() for _ in range(4)]
    P = host()
    async with AsyncExitStack() as st:
        for i, h in enumerate(servers):
            await st.enter_async_context(h.run(listen_addrs=la(base + 10 + i)))
            AutoNATService(h)
        await st.enter_async_context(P.run(listen_addrs=la(base + 20)))
        client = AutoNATService(P, serve=False)
        for h in servers:
            await P.connect(PeerInfo(h.get_id(), h.get_addrs()))
        closed = multiaddr.Multiaddr(f"/ip4/127.0.0.1/tcp/{base + 39}")  # nothing listens
        verdicts = []
        for h in servers:
            status, _ = await client.query_server(h.get_id(), addrs=[closed.to_bytes()])
            verdicts.append("OK" if status == 0 else str(status))
        client.update_status()
        name = {0: "UNKNOWN", 1: "PUBLIC", 2: "PRIVATE"}[client.get_status()]
        print(f"PROBE autonat_closed_port verdicts={verdicts} status={name} "
              "(correct answer: PRIVATE)")


async def probe_dcutr(base: int) -> None:
    out = kit.Out("probe")
    R, W, Q = host(), host(), host()
    async with AsyncExitStack() as st:
        await st.enter_async_context(R.run(listen_addrs=la(base + 30)))
        await st.enter_async_context(W.run(listen_addrs=la(base + 31)))
        await st.enter_async_context(Q.run(listen_addrs=la(base + 32)))
        relay_addr = f"/ip4/127.0.0.1/tcp/{base + 30}/p2p/{R.get_id()}"
        circuit = multiaddr.Multiaddr(f"{relay_addr}/p2p-circuit/p2p/{W.get_id()}")
        W.get_addrs = lambda: [circuit]  # W advertises no IP of its own
        rp, _ = kit.make_relay_stack(R, hop=True)
        wp, wt = kit.make_relay_stack(W, hop=False)
        qp, qt = kit.make_relay_stack(Q, hop=False)
        disc = RelayDiscovery(W, auto_reserve=True)
        wt.discovery = disc
        qd = DCUtRProtocol(Q)
        for s in (rp, wp, qp, disc, DCUtRProtocol(W), qd):
            await st.enter_async_context(background_trio_service(s))
        await kit.connect_to_relay(out, W, relay_addr)
        await kit.connect_to_relay(out, Q, relay_addr)
        await disc._add_relay(R.get_id())
        await qt.dial(circuit)
        print(f"PROBE dcutr before: {kit.describe_conns(Q, W.get_id())}")
        returned = None
        with trio.move_on_after(30):
            returned = await qd.initiate_hole_punch(W.get_id())
        await trio.sleep(1)
        print(f"PROBE dcutr initiate_hole_punch returned {returned}; "
              f"Q's connections to W now: {kit.describe_conns(Q, W.get_id())}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("probe", choices=["dht", "autonat", "dcutr"])
    p.add_argument("--base-port", type=int, default=47150)
    a = p.parse_args()
    logging.basicConfig(level=logging.CRITICAL)
    logging.getLogger("libp2p").setLevel(logging.CRITICAL)
    fn = {"dht": probe_dht, "autonat": probe_autonat, "dcutr": probe_dcutr}[a.probe]

    async def run() -> None:
        await fn(a.base_port)

    trio.run(run)


if __name__ == "__main__":
    main()
