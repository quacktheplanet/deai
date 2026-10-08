# DAI — Decentralizing the Orchestrator

> Status: design doc, 2026-09-23. Nothing here is built yet. It turns the roadmap line
> "Decentralize the orchestrator" into a concrete architecture and states what is solved,
> what is mostly assembly, and what is genuinely hard.

## The idea in one line

**Every node runs the same software. Orchestration is a role any node can hold, not a
machine that owns the network.**

There is no "orchestrator server" in the end state. A machine joins the network and,
depending on what it can do, takes on one or more roles: worker (runs inference), relay
(helps unreachable peers connect), verifier (checks other workers' results), seed (helps
newcomers find the network). Nobody is in charge, and no role is held by one machine.

## What the orchestrator does today, and what replaces each job

Today a single orchestrator (`protocol/orchestrator.py`) does five jobs. Each needs a
decentralized replacement.

| Job today | Decentralized replacement | Difficulty |
| --- | --- | --- |
| Nodes find it at a known URL | Peer discovery: a distributed hash table plus a seed list | Solved by existing libraries |
| Nodes behind NAT dial out to it | NAT traversal: relays and hole punching between peers | Solved by existing libraries |
| Picks which node runs each task | **The requester routes its own request** | Easy once the rows above work |
| Samples redundant execution, convenes committees | Requester-driven sampling; committees drawn from peers | Moderate — logic carries over; sybil resistance is the hard part |
| Keeps the ledger; alone publishes reward roots (`UPDATER_ROLE`) | Payment agreed without a trusted publisher | **Hard — the real research problem** |

The rest of this document takes them in order.

## The key simplification: the requester is the orchestrator

In most decentralized compute designs, whoever wants the work done does the routing for
their own request. A client that wants inference:

1. looks up which workers advertise the model it wants,
2. picks one by its own scoring,
3. sends the task directly,
4. optionally, quietly sends the same task to a second worker to check the first.

No one assigns jobs, because every requester orchestrates its own. Most of today's
`orchestrator.py` — scoring, dispatch, the redundant-verification sampling — moves into a
client library the requester runs. The scoring logic in `score_node()` carries over almost
unchanged; it just runs on the requester's machine instead of a central one.

## Networking

### Library choice

Both candidates are open source.

| | [py-libp2p](https://libp2p.io/releases/2026-02-16-py-libp2p/) | [Hivemind](https://github.com/learning-at-home/hivemind) |
| --- | --- | --- |
| Language | Native Python — matches this codebase | Python, over a bundled Go networking daemon |
| NAT traversal | Circuit Relay v2, hole punching (DCUtR), AutoNAT, as of v0.6.0 (Feb 2026), with a worked example of two NATed peers connecting | Yes, via the Go daemon |
| Maturity | Maintainers describe it as progressing toward production readiness | Proven: Petals ran public swarms of volunteer GPUs on it |
| Fit | General-purpose peer-to-peer | Built for decentralized training |

**Choice: py-libp2p first, Hivemind as the fallback.** It matches the language and has the
exact primitives needed. Its maturity is the risk, which is why the first milestone below
is a spike that tests it on real machines before anything is built on top.

### How peers find and reach each other

- **Discovery.** Peers find each other through a distributed hash table (Kademlia). Workers
  publish records saying which models they serve; requesters query for them.
- **Seeds.** A newcomer must know at least one existing peer's address to join. This is true
  of every peer-to-peer network — Bitcoin, BitTorrent, and IPFS all ship a seed list. It is
  not centralization provided **anyone can run a seed and several exist**. The seed list
  ships with the software and is replaceable in config.
- **Reachability.** Each peer detects whether it can be reached from the internet (AutoNAT).
  Reachable peers can offer relaying. Unreachable peers connect through a relay, then try to
  upgrade to a direct connection by hole punching (DCUtR). When hole punching fails, traffic
  stays relayed.

### The one constraint no design avoids

**Some participants must be reachable from the internet** to act as seeds and relays for
everyone behind NAT. There is no configuration of software that lets two machines behind
NAT find each other with zero reachable parties. The network provides this for itself:
any volunteer with an open port contributes it. That is the network being its own
infrastructure, not a dependence on a third party.

A likely easy source of reachable peers: many home routers let software open a port
automatically (UPnP). If py-libp2p supports automatic port mapping — **not yet verified** —
ordinary home machines could become relays with no manual setup.

## The security rules change shape

This is the part a peer-to-peer design cannot smooth over, and it needs a decision.

**Rule 1 — IP addresses are never stored.** Today this is enforced by one trusted
orchestrator coded not to log. In a permissionless peer-to-peer network, **any peer you
connect to sees your IP, and nothing stops a malicious peer from logging it.** "Never
stored" cannot be guaranteed across untrusted strangers by policy. The honest guarantee
becomes: *the fewest possible parties see your IP, transiently.*

**Rule 6 — the relay layer.** Circuit relaying implements Rule 6 almost exactly as written:
the requester talks to the worker through a relay, so the requester never learns the
worker's IP, and "only the relay does, and only transiently." But hole punching — the
feature that makes connections fast — **upgrades to a direct connection, which exposes both
IPs to each other.** Performance and IP privacy pull in opposite directions.

This needs an explicit policy, probably a per-node choice:

| Mode | Who sees a worker's IP | Cost |
| --- | --- | --- |
| **Private** — relay only, never upgrade | The relay, transiently | Slower; relays carry all traffic |
| **Direct** — allow hole punching | The relay and the requester | Faster; weaker privacy |
| **Strong** — multi-hop relaying (Tor-like) | No single party sees both ends | Slowest; most complex; later |

SECURITY.md should be updated when a mode is chosen, so the stated guarantee matches what
the architecture actually provides.

Rules 3 (no prompt content on-chain) and 5 (ephemeral, rotatable identity) carry over
cleanly: libp2p peer identities are keypairs, and rotating one is generating a new key.

## Routing

Workers advertise `(model, capability)` records in the hash table. A requester queries for
the model, receives candidates, scores them with today's `score_node()` logic, and
dispatches directly. Load balancing becomes emergent rather than central — each requester
avoids busy workers by its own scoring — which is weaker than a central router's global
view. That tradeoff is accepted for the early stages; gossip-based load signals are a later
refinement.

## Verification

Today's verification logic carries over with one change of owner: the **requester** decides
whether to sample a second worker and compares the results. The sequence the orchestrator
runs today — sample, re-run on a different worker, compare, escalate on mismatch — becomes
the requester's.

Evidence from running the current code, 2026-09-23: two nodes backed by real Llama-3.1-8B
inference agreed at 1.000 on honest tasks. A node returning divergent output was caught at
0.237 against a 0.850 threshold. Committee escalation convened but could not proceed —
"0/2 eligible nodes" — so it needs more nodes online, not more code.

What gets **harder** without a trusted coordinator:

- **Collusion and sybils.** Today the orchestrator picks the checker, so a cheating worker
  cannot choose who checks it. Peer-to-peer, if an attacker runs both the worker *and* the
  checker, redundant execution proves nothing. Committees drawn at random from the hash
  table are vulnerable to an attacker who floods the table with identities. The planned
  vesting-bond sybil resistance (ECONOMICS.md) becomes a prerequisite, not an enhancement.
- **Honest variance across hardware.** The 1.000 agreement above is partly because both nodes
  used the same backend on the same machine. Two honest nodes on *different* hardware may not
  produce identical output even at temperature 0. The placeholder comparator (sequence ratio)
  would flag that as a mismatch and slash an honest provider. The decided semantic-embedding
  comparator must be calibrated on real cross-hardware data before slashing is ever turned
  on, centralized or not.

## Payment: the hard problem

Today the orchestrator alone holds `UPDATER_ROLE` and publishes cumulative reward roots to
`MerkleDistributor`. Remove the orchestrator and someone still has to say who earned what,
without being trusted.

The most promising direction starts from a contract that already exists:

**Requester-funded receipts.** The requester deposits into `PaymentContract` escrow (written
and tested, not yet wired into the live path). When it receives a satisfactory result, it
signs a receipt: "worker W completed task T." The worker submits signed receipts to the
contract to claim. No orchestrator is in the payment loop at all.

This matters beyond removing the orchestrator. Today rewards are **minted** — created
rather than paid. In a network with no trusted root publisher, minted rewards for work a
requester vouches for are free money: an attacker runs a requester and a worker, "requests"
work from themselves, signs receipts, and farms tokens. **When the requester pays from
their own escrow, self-dealing just moves their own money around, and the exploit
disappears.** Decentralization effectively requires moving from minted to user-funded
payment.

Open problems with this direction:

- **A requester can refuse to sign** after receiving a good result. Mitigations: pay per small
  chunk rather than per task, so the most a worker can lose is one chunk; reputation for
  requesters who stiff workers.
- **Bootstrapping subsidy.** ECONOMICS.md plans subsidized compute to seed demand. Any
  subsidy is minted, which reintroduces the self-dealing exploit. Subsidy must be bounded and
  tied to verified work, not to receipts alone.
- **On-chain cost.** Claiming per receipt is expensive. Batching receipts, or an
  optimistic-rollup-style scheme with a challenge window, reduces cost at the price of
  complexity.

This is the one row of the job table that is research rather than assembly. It should be
worked on in parallel with the networking, not after it.

## The launcher

"Any modern machine can run it" is mostly a launcher problem, and every step of it was done
by hand on 2026-09-23:

1. Detect the OS, GPU vendor, VRAM and RAM.
2. Download the matching llama.cpp release — builds exist for CUDA, Vulkan, Metal and CPU.
3. Pick a model that fits available memory. CPU-only machines get a small model, so even a
   laptop without a GPU can participate.
4. Create a Python virtual environment and install the dependencies.
5. Generate a peer identity keypair.
6. Join the network through the seed list, and offer relaying if the machine is reachable.

The node already supports any OpenAI-compatible inference backend (not only Ollama), which
is what lets the launcher standardize on llama.cpp. This is independent of the networking
work and can be built in parallel.

## The staircase

The same approach as VISION.md: each step is independently useful and provable before the
next.

| Step | What changes | Still centralized |
| --- | --- | --- |
| **D0 — today** | One orchestrator routes, verifies, and publishes reward roots | Everything |
| **D1 — federated** | Anyone runs an orchestrator; nodes choose which to join (already supported — see README) | Each orchestrator over its own nodes |
| **D2 — peer-to-peer transport** | libp2p discovery and NAT traversal; requesters route their own tasks | Payment still settled by an orchestrator |
| **D3 — requester-funded receipts** | `PaymentContract` escrow wired in; workers claim with signed receipts | Nothing in the routing or payment path |
| **D4 — decentralized verification** | Random peer committees, protected by vesting-bond sybil resistance | Nothing |

This is a second axis alongside VISION.md's staircase, not a replacement. VISION.md is about
*how the compute is split*; this is about *who coordinates it*. The two are independent:
single-node inference (VISION Stage 0) can run on a fully decentralized network (D4).

One observation that bears on VISION.md: by 2026, quantized models run on single consumer
GPUs that would once have needed a Petals-style swarm, and at least one retrospective argues
[model-sharded inference struggles in practice](https://explainx.ai/blog/petals-distributed-llm-inference-revisited-july-2026).
That makes decentralized coordination of whole-model inference (this document) more
important in the near term than model sharding (VISION Stage 2).

## First milestone

**A spike, not a build.** Prove the networking works on real machines before designing
anything on top of it.

- **Setup:** this machine (a cloud VM behind NAT, cannot accept inbound connections) and a
  home laptop (behind home NAT), running py-libp2p's relay / listener / dialer example.
- **Check each primitive** the design depends on, rather than assuming it: relayed
  connection, hole-punch upgrade to direct, AutoNAT reachability detection, Kademlia
  discovery, and whether automatic port mapping (UPnP) exists.
- **Success:** two machines on different networks exchange messages peer-to-peer with no
  third-party service in the path, and the relay step shows exactly who sees which IP.
- **If it fails:** record which primitive broke, then repeat the spike with Hivemind before
  concluding anything about the design.

This spike also answers the practical question that started this document — getting two
separate machines working together — in the decentralized way rather than by putting an
orchestrator on a public server.

**Status (2026-10-08): local half done, two-machine half next.** The kit and full results
are in [spike/p2p/README.md](../spike/p2p/README.md). On one machine (py-libp2p 0.8.0,
separate processes on 127.0.0.1):

- **Work:** relay reservation, connection and messaging through the relay, and Kademlia
  discovery (a "serves model X" record, plus two peers finding a third through the seed).
- **Can't be judged on one host:** hole punching (DCUtR). A real test needs the relay on a
  third network.
- **Broken in 0.8.0:** AutoNAT reports every peer as publicly reachable.
- **UPnP:** exists in the library. Untestable on the pod, which has no router.

IP privacy: in private mode the requester and worker each saw only the relay. But ordinary
DHT traffic dials peers directly, so private mode must cover DHT traffic too.
py-libp2p 0.8.0 installs natively on Windows with Python 3.12 or 3.13. Next step: the
laptop acts as the reachable relay, through UPnP or a port forward.

## Open questions

- **Payment publisher.** Requester-funded receipts are the leading direction, but the
  stiffing, subsidy, and on-chain cost problems above are unsolved.
- **IP privacy mode.** Private, direct, or strong — or per-node choice. SECURITY.md must
  change to match.
- **Seed list governance.** Who can add seeds to the default list, and how is it updated
  without a single controlling party?
- **py-libp2p maturity.** Whether it is ready for this is what the spike exists to find out.
- **Load balancing** without a global view, beyond per-requester scoring.

## Sources

- [py-libp2p v0.6.0 release announcement](https://libp2p.io/releases/2026-02-16-py-libp2p/)
  and [release notes](https://py-libp2p.readthedocs.io/en/stable/release_notes.html)
- [Hivemind](https://github.com/learning-at-home/hivemind)
- [Petals overview](https://deepwiki.com/bigscience-workshop/petals/1-overview)
- [Petals distributed LLM inference, revisited (July 2026)](https://explainx.ai/blog/petals-distributed-llm-inference-revisited-july-2026)
