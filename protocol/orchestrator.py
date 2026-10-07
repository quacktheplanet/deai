"""
DAI Orchestrator
-----------------
The network brain. Responsibilities:
  - Accept inference requests via HTTP (OpenAI-compatible API)
  - Maintain a registry of connected Worker Nodes
  - Dispatch tasks to the best available node (scored routing)
  - Collect results, run pluggable verification, return to caller
  - Track basic network stats and earnings

Routing score (higher = better candidate):
  +20  exact model match
  + 1  "any" model fallback
  + 5  idle longest (round-robin tiebreaker, max 5pts)

  Hardware (GPU / VRAM) is deliberately NOT scored: it is self-reported and
  unverified, so rewarding or prioritizing it pays for an unprovable claim —
  the same anti-pattern the verification work removed from `mock_verify`. A
  node's real capability will instead come from measured, verified delivered
  work (the deferred benchmark/tier system — see docs/VERIFICATION.md
  build-now #3 and docs/VERIFICATION_PROTOCOL.md §1).

Modes:
  Mock (default) — in-memory ledger, no blockchain required
    python protocol/orchestrator.py

  On-chain — rewards accrue off-chain, settled per epoch via a Merkle
  root; SlashingContract for reputation + eligibility
    python protocol/orchestrator.py --chain \\
        --rpc-url http://localhost:8545 \\
        --token-contract 0x... \\
        --slashing-contract 0x... \\
        --payment-contract 0x... \\
        --distributor-contract 0x... \\
        --orchestrator-key 0x...

  See docs/CHAIN_SETUP.md for full on-chain setup instructions.
"""

import argparse
import asyncio
import json
import logging
import random
import sys
import time
import os
from typing import Callable, Dict, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv optional; env vars can be set by the shell instead

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.schemas import (
    ChatRequest, ChatResponse, Choice, Message, Role, Usage,
    Task, TaskResult, TaskStatus,
    NodeInfo, NodeStatus,
    WSMessage,
)
from ledger import Ledger
from verification import (
    Verifier, ContentVerifier, RedundantExecutionVerifier, ComparatorUnavailable, make_verifier,
)
from model_registry import ModelRegistry, ModelStack
from committee import (
    CommitteeOutcome, Verdict, quorum, tally_votes,
    DEFAULT_COMMITTEE_SIZE, DEFAULT_COMMITTEE_TIMEOUT_S,
    DEFAULT_APPEAL_WINDOW_S, DEFAULT_SLASH_FRACTION,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("orchestrator")

@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Load the embedding model before the first comparison needs it (a cold
    # load can exceed the comparator's normal timeout).
    warm = getattr(getattr(verifier, "_comparator", None), "warm_up", None)
    if warm is not None:
        asyncio.create_task(asyncio.to_thread(warm))
    if chain_ledger is not None:
        async def _loop():
            while True:
                await asyncio.sleep(_settle_interval)
                try:
                    await asyncio.to_thread(chain_ledger.settle_epoch)
                except Exception as e:
                    log.error(f"settlement loop error  err={e}")
        asyncio.create_task(_loop())
        log.info(f"Reward settlement loop started  interval={_settle_interval}s")
    yield


app = FastAPI(title="DAI Orchestrator", version="0.1.0", lifespan=_lifespan)


# ── State ─────────────────────────────────────────────────────────────────────

# Set at startup from CLI args — None means mock/dev mode
chain_ledger = None

# Reward-settlement epoch length (seconds). Chain mode only.
_settle_interval = 3600

# Per-task inference timeout (seconds). Raise via --task-timeout for slow
# CPU nodes or large models. GPU nodes typically finish well under 60s.
_task_timeout = 60.0

# Extra time given to a node that reports its model is not loaded (a sleeping
# node, see compute/node.py --sleep-after). A cold 14B load took 72-75 s.
_cold_start_allowance = 240.0

# Optional API key — None means open access (fine for local dev)
_api_key: Optional[str] = None
_bearer = HTTPBearer(auto_error=False)


def _check_api_key(creds: Optional[HTTPAuthorizationCredentials] = Security(_bearer)):
    if _api_key is None:
        return  # open access
    if creds is None or creds.credentials != _api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")

class NodeConnection:
    def __init__(self, ws: WebSocket, info: NodeInfo):
        self.ws = ws
        self.info = info
        self.status = NodeStatus.idle
        self.last_seen = time.time()
        self.last_task_time: float = 0.0  # for round-robin tiebreaking
        self.tasks_completed = 0
        self.current_task_id: Optional[str] = None


# node_id → NodeConnection
nodes: Dict[str, NodeConnection] = {}

# node_ids already warned as ineligible — prevents log spam when the same
# ejected node stays connected through the 30-second dispatch polling loop.
_warned_ineligible: set = set()

# task_id → asyncio.Event  (set when result arrives)
pending_events: Dict[str, asyncio.Event] = {}

# task_id → TaskResult
results: Dict[str, TaskResult] = {}

# simple stats. Of the completed tasks: verified = a second node's result was
# compared and agreed; unverified = a recheck was attempted but could not
# decide (no free checker, comparator down, no committee quorum) and the task
# was accepted optimistically; unchecked = no recheck (not sampled, content-only
# verifier, or the model has no registered stack).
stats = {"requests": 0, "completed": 0, "failed": 0,
         "verified": 0, "unverified": 0, "unchecked": 0}

# earnings ledger
ledger = Ledger()

# Verification policy. Default = ContentVerifier (legacy non-empty check, no
# recheck) so mock mode and CI are unchanged. --verify-sample-rate swaps in the
# optimistic redundant-execution verifier. See docs/VERIFICATION.md.
verifier: Verifier = ContentVerifier()

# Reference inference stack registry (VERIFICATION_PROTOCOL.md §12).
# Models without a registered stack always run ContentVerifier regardless of
# --verify-sample-rate.  Register stacks via POST /admin/model-registry.
model_registry: ModelRegistry = ModelRegistry()

# Committee escalation (VERIFICATION_PROTOCOL.md §13). Configurable via CLI;
# defaults are the §13.8 starting points.
_committee_size: int = DEFAULT_COMMITTEE_SIZE
_committee_timeout: float = DEFAULT_COMMITTEE_TIMEOUT_S
_appeal_window: float = DEFAULT_APPEAL_WINDOW_S
_slash_fraction: float = DEFAULT_SLASH_FRACTION


# ── Routing ───────────────────────────────────────────────────────────────────

def score_node(node: NodeConnection, model: str, project: Optional[str] = None) -> int:
    """
    Score a node as a candidate for a given model + project request.
    Returns -1 if the node cannot or will not handle this request.

    Project routing:
      - Dedicated node (node.info.project set): only accepts tasks for its
        declared project. A task with no project, or a different project,
        scores -1.
      - General node (node.info.project None): accepts all tasks regardless
        of project tag.
    """
    # Project gate — must check before model so dedicated nodes are excluded
    # cleanly even if they advertise "any" model.
    node_project = node.info.project
    if node_project is not None and node_project != project:
        return -1

    wildcard = model == "any"
    can_run = wildcard or model in node.info.models or "any" in node.info.models
    if not can_run:
        return -1

    score = 0

    # Exact model match strongly preferred over wildcard or generic "any" nodes
    if not wildcard and model in node.info.models:
        score += 20
    else:
        score += 1

    # Dedicated project match bonus — prefer the node purpose-built for this
    # project over a general node that happens to be idle.
    if project is not None and node_project == project:
        score += 5

    # A sleeping node (model unloaded) answers only after a load of a minute or
    # more; prefer any warm node, and let sleeping ones keep the GPU for their
    # owner unless nobody else can serve the request.
    if node.info.warm:
        score += 10

    # Hardware (gpu / vram_gb) intentionally not scored — self-reported and
    # unverified. See module docstring and docs/VERIFICATION.md build-now #3.

    # Round-robin tiebreaker: prefer the node that has been idle longest.
    # Use a float ratio so even millisecond differences break ties — prevents
    # the same node from monopolising primary slots when all nodes finish
    # at nearly the same time.
    idle_seconds = time.time() - node.last_task_time
    score += min(idle_seconds / 10.0, 5.0)

    return score


def find_best_node(
    model: str, exclude: Optional[set] = None, project: Optional[str] = None
) -> Optional[tuple[NodeConnection, int, str]]:
    """
    Return (best_node, score, reason) for the given model + project, or None.
    Only considers idle nodes. `exclude` is a set of node_ids to skip — used by
    redundant verification so the recheck lands on a *different* node than the
    one that produced the primary result. In chain mode, also skips miners that
    have been ejected from the SlashingContract (stake burned below minimum).
    """
    exclude = exclude or set()
    candidates = []
    for node in nodes.values():
        if node.status != NodeStatus.idle:
            continue
        if node.info.node_id in exclude:
            continue
        if chain_ledger is not None and node.info.wallet:
            if not chain_ledger.is_eligible(node.info.wallet):
                if node.info.node_id not in _warned_ineligible:
                    log.warning(f"Node ineligible (ejected on-chain)  id={node.info.node_id}  wallet={node.info.wallet[:10]}...")
                    _warned_ineligible.add(node.info.node_id)
                continue
        s = score_node(node, model, project)
        if s >= 0:
            candidates.append((s, node))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    best_score, best_node = candidates[0]

    parts = []
    if model in best_node.info.models:
        parts.append("exact-model")
    else:
        parts.append("any-model")
    if best_node.info.project:
        parts.append(f"project={best_node.info.project}")
    if len(candidates) > 1:
        parts.append(f"{len(candidates)}-candidates")

    return best_node, best_score, "+".join(parts)


# ── WebSocket: Node registration & task loop ──────────────────────────────────

@app.websocket("/ws/node")
async def node_endpoint(ws: WebSocket):
    await ws.accept()
    node_id = None

    try:
        # First message must be a register payload
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
        msg = WSMessage(**json.loads(raw))

        if msg.type != "register":
            await ws.send_text(json.dumps({"type": "error", "payload": {"detail": "First message must be 'register'"}}))
            await ws.close()
            return

        info = NodeInfo(**msg.payload)
        node_id = info.node_id
        nodes[node_id] = NodeConnection(ws=ws, info=info)

        log.info(f"Node joined  id={node_id}  models={info.models}  gpu={info.gpu}")
        await ws.send_text(json.dumps({"type": "ack", "payload": {"node_id": node_id, "message": "Registered. Waiting for tasks."}}))

        # Keep connection alive and handle incoming messages
        while True:
            raw = await ws.receive_text()
            msg = WSMessage(**json.loads(raw))

            if msg.type == "heartbeat":
                nodes[node_id].last_seen = time.time()

            elif msg.type == "status":
                # A node going to sleep (model unloaded) or waking up.
                warm = bool(msg.payload.get("warm", True))
                if warm != nodes[node_id].info.warm:
                    nodes[node_id].info.warm = warm
                    log.info(f"Node {'warm ' if warm else 'asleep'}  id={node_id}")

            elif msg.type == "task_complete":
                result = TaskResult(**msg.payload)

                node = nodes[node_id]
                node.status = NodeStatus.idle
                node.tasks_completed += 1
                node.current_task_id = None
                results[result.task_id] = result

                log.info(f"Result in    id={result.task_id}  node={node_id}  tokens={result.tokens_used}")

                # Verification, ledger, and on-chain settlement happen in the
                # HTTP path — it owns the task context and can re-dispatch for
                # a redundant check before any reward is finalized. Here we
                # just hand the raw result back to the waiting request.
                if result.task_id in pending_events:
                    pending_events[result.task_id].set()

            elif msg.type == "task_failed":
                task_id = msg.payload.get("task_id")
                node = nodes[node_id]
                node.status = NodeStatus.idle
                stats["failed"] += 1
                log.warning(f"Task failed  id={task_id}  node={node_id}")
                if task_id and task_id in pending_events:
                    pending_events[task_id].set()

    except asyncio.TimeoutError:
        log.warning("Node timed out during registration")
    except WebSocketDisconnect:
        log.info(f"Node disconnected  id={node_id}")
    except Exception as e:
        log.exception(f"Node error  id={node_id}  err={e}")
    finally:
        if node_id and node_id in nodes:
            node = nodes.pop(node_id)
            _warned_ineligible.discard(node_id)
            log.info(f"Node removed  id={node_id}  remaining={len(nodes)}")
            # Wake any in-flight task immediately so the caller gets a 504
            # in milliseconds rather than waiting out the 60-second timeout.
            if node.current_task_id and node.current_task_id in pending_events:
                pending_events[node.current_task_id].set()


# ── Dispatch ──────────────────────────────────────────────────────────────────

async def _dispatch_and_wait(
    task: Task, exclude: Optional[set] = None
) -> tuple[Optional[TaskResult], Optional[NodeConnection], str]:
    """
    Find an idle node (skipping `exclude`), dispatch the task, await the result.
    Returns (result, node, status) where status is 'ok' | 'no_node' | 'timeout'.
    On anything other than 'ok', result and node are None.
    """
    # Fast-fail when no nodes are registered at all — no point polling.
    # Only wait if nodes exist but are currently busy (one may free up soon).
    if not nodes:
        return None, None, "no_node"

    match = None
    deadline = time.time() + 30
    while time.time() < deadline:
        match = find_best_node(task.model, exclude, task.project)
        if match:
            break
        await asyncio.sleep(0.5)

    if match is None:
        return None, None, "no_node"

    node, score, reason = match
    node.status = NodeStatus.busy
    node.last_task_time = time.time()
    node.current_task_id = task.task_id
    event = asyncio.Event()
    pending_events[task.task_id] = event

    dispatch_msg = {
        "type": "task",
        "payload": {
            "task_id": task.task_id,
            "model": task.model,
            "messages": [m.model_dump() for m in task.messages],
            "max_tokens": task.max_tokens,
            "temperature": task.temperature,
            "seed": task.seed,  # None if no registered stack; node ignores None
        }
    }
    await node.ws.send_text(json.dumps(dispatch_msg))
    timeout = _task_timeout if node.info.warm else _task_timeout + _cold_start_allowance
    log.info(f"Dispatched   id={task.task_id}  to={node.info.node_id}  score={score}  reason={reason}"
             + ("" if node.info.warm else f"  (asleep: allowing {timeout:.0f}s to load)"))

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pending_events.pop(task.task_id, None)
        node.status = NodeStatus.idle
        return None, None, "timeout"

    pending_events.pop(task.task_id, None)
    result = results.pop(task.task_id, None)
    if result is None:
        return None, None, "timeout"
    return result, node, "ok"


def _slash_for_bad_result(node: NodeConnection, reason: str) -> None:
    """
    Unambiguously bad result (e.g. empty). Preserves the prior on-chain slash.
    A redundant-verification *mismatch* does NOT come here — that path requires
    committee escalation and must not auto-slash (see chat_completions and
    docs/VERIFICATION.md on false-positive slashing).
    """
    log.warning(f"Bad result   node={node.info.node_id}  reason={reason}")
    if chain_ledger is not None and node.info.wallet:
        asyncio.create_task(asyncio.to_thread(chain_ledger.slash_onchain, node.info.wallet))


# ── Committee escalation (VERIFICATION_PROTOCOL §13) ─────────────────────────

def _select_committee_nodes(
    model: str, n: int, exclude_ids: set, project: Optional[str] = None
) -> list[NodeConnection]:
    """§13.2: pick up to ``n`` idle eligible nodes, excluding the primary and
    checker, *uniformly at random within the eligible pool* to defeat a
    predictable-committee attack. Marks the picks busy synchronously so
    parallel selection has no race.

    Refinements deferred (§13.2): preferring low-dispute-rate and
    longest-active nodes once per-node dispute history is tracked.
    """
    candidates: list[NodeConnection] = []
    for node in nodes.values():
        if node.status != NodeStatus.idle:
            continue
        if node.info.node_id in exclude_ids:
            continue
        if chain_ledger is not None and node.info.wallet:
            if not chain_ledger.is_eligible(node.info.wallet):
                continue
        if score_node(node, model, project) < 0:
            continue
        candidates.append(node)
    random.shuffle(candidates)
    picked = candidates[:n]
    now = time.time()
    for node in picked:
        node.status = NodeStatus.busy
        node.last_task_time = now
    return picked


async def _ask_committee_member(node: NodeConnection, task: Task) -> Optional[TaskResult]:
    """Dispatch ``task`` (fresh task_id) to ``node`` and await its reply within
    ``_committee_timeout``. Returns None on send error or timeout. The node is
    released to idle whether or not it responded."""
    event = asyncio.Event()
    pending_events[task.task_id] = event
    node.current_task_id = task.task_id
    msg = {
        "type": "task",
        "payload": {
            "task_id": task.task_id,
            "model": task.model,
            "messages": [m.model_dump() for m in task.messages],
            "max_tokens": task.max_tokens,
            "temperature": task.temperature,
            "seed": task.seed,
        },
    }
    try:
        await node.ws.send_text(json.dumps(msg))
    except Exception as e:
        log.warning(f"COMMITTEE dispatch failed  node={node.info.node_id}  err={e}")
        pending_events.pop(task.task_id, None)
        node.status = NodeStatus.idle
        return None
    timeout = _committee_timeout if node.info.warm else _committee_timeout + _cold_start_allowance
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pending_events.pop(task.task_id, None)
        node.status = NodeStatus.idle
        return None
    pending_events.pop(task.task_id, None)
    return results.pop(task.task_id, None)


async def _convene_committee(
    base_task: Task,
    primary: TaskResult,
    checker: TaskResult,
    primary_node: NodeConnection,
    checker_node: NodeConnection,
    comparator: Callable[[str, str], float],
    threshold: float,
) -> Optional[CommitteeOutcome]:
    """§13.3–§13.4: convene ``_committee_size`` nodes, gather their responses,
    return the verdict. Returns None when fewer than quorum nodes either could
    be selected or returned in time (caller falls back to ``FINALIZED*`` —
    optimistic accept, §13.3)."""
    exclude = {primary_node.info.node_id, checker_node.info.node_id}
    members = _select_committee_nodes(
        base_task.model, _committee_size, exclude, base_task.project
    )
    q = quorum(_committee_size)
    if len(members) < q:
        for node in members:
            node.status = NodeStatus.idle
        log.warning(
            f"COMMITTEE  id={base_task.task_id}  insufficient eligible nodes "
            f"({len(members)}/{q}) — fallback to FINALIZED*"
        )
        return None

    log.info(f"COMMITTEE  id={base_task.task_id}  convening N={len(members)}")
    shadows = [
        Task(model=base_task.model, messages=base_task.messages,
             max_tokens=base_task.max_tokens, temperature=base_task.temperature,
             seed=base_task.seed, project=base_task.project)
        for _ in members
    ]
    responses = await asyncio.gather(
        *(_ask_committee_member(n, t) for n, t in zip(members, shadows)),
        return_exceptions=False,
    )
    contents = [r.content for r in responses if r is not None and r.content]
    if len(contents) < q:
        log.warning(
            f"COMMITTEE  id={base_task.task_id}  only {len(contents)} responses "
            f"(need {q}) — fallback to FINALIZED*"
        )
        return None

    try:
        return await asyncio.to_thread(
            tally_votes, primary.content, checker.content, contents, comparator, threshold
        )
    except ComparatorUnavailable as e:
        log.warning(
            f"COMMITTEE  id={base_task.task_id}  comparator unavailable ({e}) "
            f"— fallback to FINALIZED*"
        )
        return None


def _apply_slash(node_id: str, wallet: Optional[str], fraction: float, reason: str) -> None:
    """§13.5: burn ``fraction`` of the node's unvested bond off-chain (in-memory
    ledger), plus reduce the off-chain accrual bond and call the on-chain
    ``SlashingContract.slash`` when running with chain mode. Idempotent on
    zero-balance nodes (slash returns 0)."""
    burned = ledger.slash(node_id, fraction, reason)
    log.warning(
        f"SLASH        node={node_id}  fraction={fraction:.2%}  "
        f"burned_offchain={burned:.2f} DAI  reason={reason}"
    )
    if chain_ledger is not None and wallet:
        try:
            chain_ledger.slash_accrual(wallet, fraction)
        except Exception as e:
            log.error(f"slash_accrual failed  wallet={wallet}  err={e}")
        asyncio.create_task(asyncio.to_thread(chain_ledger.slash_onchain, wallet))


async def _schedule_slash(node_id: str, wallet: Optional[str], fraction: float,
                          reason: str, window: float) -> None:
    """§13.6: appeal window (time-lock; appeal mechanism itself unbuilt).
    Sleeps ``window`` seconds then applies the slash. With window=0 the slash
    is immediate (useful for tests)."""
    if window > 0:
        log.warning(
            f"SLASH queued node={node_id}  reason={reason}  "
            f"appeal_window={window:.0f}s"
        )
        await asyncio.sleep(window)
    _apply_slash(node_id, wallet, fraction, reason)


# ── HTTP: OpenAI-compatible inference endpoint ────────────────────────────────

@app.post("/v1/chat/completions", response_model=ChatResponse)
async def chat_completions(req: ChatRequest, response: Response, _auth=Depends(_check_api_key)):
    stats["requests"] += 1

    # Apply registered stack parameters if this model has one (§12.4).
    # temperature=0 and the registered seed override the request values so both
    # primary and checker run identically — required for comparison to be valid.
    stack = model_registry.get(req.model)
    task = Task(
        model=req.model,
        messages=req.messages,
        max_tokens=max(req.max_tokens if req.max_tokens is not None else 512, stack.max_tokens) if stack else (req.max_tokens if req.max_tokens is not None else 512),
        temperature=stack.temperature if stack else (req.temperature if req.temperature is not None else 0.7),
        seed=stack.seed if stack else None,
        project=req.project,
    )

    # Registry-gated verifier selection (§12.5): only registered+eligible models
    # use the Standard-tier verifier; all others fall back to ContentVerifier.
    if model_registry.is_eligible(task.model):
        effective_verifier: Verifier = verifier
    else:
        if isinstance(verifier, RedundantExecutionVerifier):
            log.info(
                f"Model '{task.model}' has no registered stack — "
                "falling back to ContentVerifier. "
                "Register via POST /admin/model-registry to enable Standard-tier verification."
            )
        effective_verifier = ContentVerifier()

    log.info(f"Request recv  id={task.task_id}  model={req.model}  nodes_online={len(nodes)}")

    primary, primary_node, status = await _dispatch_and_wait(task)
    if status == "no_node":
        stats["failed"] += 1
        raise HTTPException(
            status_code=503,
            detail=f"No nodes available for model '{req.model}'. Try again shortly or connect a node.",
        )
    if status == "timeout":
        stats["failed"] += 1
        raise HTTPException(status_code=504, detail="Node did not respond in time.")

    # Cheap local gate — replaces the old mock_verify non-empty check.
    if not effective_verifier.well_formed(primary):
        stats["failed"] += 1
        _slash_for_bad_result(primary_node, "malformed/empty result")
        raise HTTPException(status_code=500, detail="Task result failed verification.")

    # Optimistic redundant check (Standard tier, docs/VERIFICATION.md). Sampled
    # and silent: the node is not told it is being checked.
    verification = "unchecked"
    if effective_verifier.should_recheck(task, primary):
        verification = "unverified"
        shadow = Task(
            model=task.model,
            messages=task.messages,
            max_tokens=task.max_tokens,
            temperature=task.temperature,
            seed=task.seed,
        )
        r2, n2, s2 = await _dispatch_and_wait(
            shadow, exclude={primary_node.info.node_id}
        )
        if s2 != "ok" or not effective_verifier.well_formed(r2):
            # No independent checker free — cannot verify. Accept optimistically
            # rather than punish a provider for a thin network; record that this
            # task went unverified.
            log.warning(f"VERIFY skip   id={task.task_id}  reason=no-checker-available")
        else:
            # In a thread: an embedding comparator makes a blocking HTTP call
            # that can take a while when its model is cold.
            outcome = await asyncio.to_thread(effective_verifier.compare, task, primary, r2)
            label = "unverified" if outcome.unverified else ("OK " if outcome.accepted else "MISMATCH")
            log.info(
                f"VERIFY {label}  "
                f"id={task.task_id}  primary={primary_node.info.node_id}  "
                f"checker={n2.info.node_id}  {outcome.detail}"
            )
            if outcome.accepted and not outcome.unverified:
                verification = "verified"
            if not outcome.accepted:
                # §13: convene a committee, decide by majority, and only on a
                # confirmed dishonest verdict schedule a delayed slash. A
                # 2-sample mismatch never auto-slashes — false-positive
                # slashing of an honest provider is flagged existential.
                committee_outcome = await _convene_committee(
                    task, primary, r2, primary_node, n2,
                    effective_verifier._comparator,
                    effective_verifier.agreement_threshold,
                )
                if committee_outcome is None:
                    # §13.3 fallback: insufficient committee → FINALIZED*.
                    log.warning(
                        f"VERIFY unverified  id={task.task_id}  "
                        f"reason=committee-unavailable; accepting optimistically"
                    )
                elif committee_outcome.verdict is Verdict.PRIMARY_UPHELD:
                    log.info(
                        f"COMMITTEE  id={task.task_id}  verdict=PRIMARY_UPHELD "
                        f"{committee_outcome.detail}  dishonest=checker({n2.info.node_id})"
                    )
                    asyncio.create_task(_schedule_slash(
                        n2.info.node_id, n2.info.wallet, _slash_fraction,
                        f"committee verdict CHECKER dishonest on task {task.task_id}",
                        _appeal_window,
                    ))
                    verification = "verified"
                    # primary wins → fall through to accrual.
                elif committee_outcome.verdict is Verdict.CHECKER_UPHELD:
                    log.warning(
                        f"COMMITTEE  id={task.task_id}  verdict=CHECKER_UPHELD "
                        f"{committee_outcome.detail}  dishonest=primary({primary_node.info.node_id})"
                    )
                    asyncio.create_task(_schedule_slash(
                        primary_node.info.node_id, primary_node.info.wallet, _slash_fraction,
                        f"committee verdict PRIMARY dishonest on task {task.task_id}",
                        _appeal_window,
                    ))
                    stats["failed"] += 1
                    raise HTTPException(
                        status_code=502,
                        detail=(
                            f"Committee upheld checker; primary result rejected "
                            f"({committee_outcome.detail})"
                        ),
                    )
                else:  # UNRESOLVABLE — tie or no majority. No slash, no pay.
                    log.warning(
                        f"COMMITTEE  id={task.task_id}  verdict=UNRESOLVABLE "
                        f"{committee_outcome.detail}"
                    )
                    stats["failed"] += 1
                    raise HTTPException(
                        status_code=502,
                        detail=f"Verification unresolvable ({committee_outcome.detail})",
                    )

    # Accepted → finalize the reward (off-chain ledger + optional on-chain mint).
    # Unverified tasks are paid too (optimistic, as with no free checker); the
    # header and stats say which it was.
    primary.verified = verification == "verified"
    stats["completed"] += 1
    stats[verification] += 1
    response.headers["X-DAI-Verification"] = verification
    earned = ledger.record_completion(
        node_id=primary_node.info.node_id,
        task_id=primary.task_id,
        output_tokens=primary.tokens_used,
    )
    log.info(
        f"Task done    id={primary.task_id}  node={primary_node.info.node_id}  "
        f"tokens={primary.tokens_used}  earned={earned:.2f}"
    )

    if chain_ledger is not None and primary_node.info.wallet:
        asyncio.create_task(
            asyncio.to_thread(
                chain_ledger.record_completion_onchain,
                primary_node.info.wallet,
                primary.tokens_used,
            )
        )

    prompt_tokens = sum(len(m.content.split()) for m in req.messages)
    completion_tokens = len(primary.content.split())

    return ChatResponse(
        model=req.model,
        choices=[
            Choice(
                message=Message(role=Role.assistant, content=primary.content),
                finish_reason="stop",
            )
        ],
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


# ── HTTP: Network status dashboard ───────────────────────────────────────────

@app.get("/status")
def network_status():
    now = time.time()
    return {
        "nodes_online": len(nodes),
        "nodes": [
            {
                "node_id": n.info.node_id,
                "models": n.info.models,
                "gpu": n.info.gpu,
                "vram_gb": n.info.vram_gb,
                "status": n.status.value,
                "warm": n.info.warm,
                "tasks_completed": n.tasks_completed,
                "balance": ledger.balance(n.info.node_id),
                "score": score_node(n, n.info.models[0]) if n.status == NodeStatus.idle else "busy",
                "last_seen_ago_s": round(now - n.last_seen, 1),
                "last_task_ago_s": round(now - n.last_task_time, 1) if n.last_task_time else None,
            }
            for n in nodes.values()
        ],
        "stats": stats,
        "economy": ledger.network_totals(),
    }


@app.get("/earnings")
def earnings():
    """Leaderboard — all nodes ranked by total earnings."""
    return {
        "leaderboard": ledger.all_balances(),
        "economy": ledger.network_totals(),
    }


@app.get("/earnings/{node_id}")
def node_earnings(node_id: str):
    """Earnings detail for a specific node."""
    return ledger.summary(node_id)


class _ClaimSubmit(BaseModel):
    signed_tx: str  # raw signed transaction hex, with or without 0x prefix


@app.get("/claim/{wallet}")
def claim_get(wallet: str):
    """
    Return a miner's pending reward info: cumulative amount, Merkle proof,
    already-claimed amount, ABI-encoded calldata, and gas estimate.

    The miner signs a tx using calldata + distributor_contract (with any
    wallet tool — cast, MetaMask, Python, etc.) and POSTs it back to
    POST /claim/{wallet} for broadcasting. No web3 tooling required on the
    miner side beyond signing.

    404 in mock mode or before this wallet appears in a settled root.
    """
    if chain_ledger is None:
        raise HTTPException(status_code=404, detail="Not in chain mode — no on-chain settlement.")
    info = chain_ledger.claim_info(wallet)
    if info is None:
        raise HTTPException(status_code=404, detail="No settled rewards for this wallet yet.")
    return info


@app.post("/claim/{wallet}")
async def claim_post(wallet: str, body: _ClaimSubmit):
    """
    Broadcast a miner's signed claim transaction.

    The miner constructs and signs the tx locally using the calldata returned
    by GET /claim/{wallet}, then POSTs the raw signed hex here. The
    orchestrator relays it to the chain and returns the tx hash.

    MerkleDistributor.claim() requires msg.sender == miner wallet, so the
    miner must sign with their own key — the orchestrator cannot claim on
    their behalf.
    """
    if chain_ledger is None:
        raise HTTPException(status_code=404, detail="Not in chain mode.")
    try:
        tx_hash = await asyncio.to_thread(chain_ledger.broadcast_tx, body.signed_tx)
        log.info(f"Claim broadcast  wallet={wallet[:10]}...  tx={tx_hash[:18]}...")
        return {"tx_hash": tx_hash, "status": "broadcast"}
    except Exception as e:
        log.error(f"Claim broadcast failed  wallet={wallet}  err={e}")
        raise HTTPException(status_code=400, detail=f"Broadcast failed: {e}")


# ── Admin: Reference Inference Stack Registry (VERIFICATION_PROTOCOL.md §12) ──

class _StackRegisterRequest(BaseModel):
    model_id: str
    runtime: str
    format: str = ""
    temperature: float = 0.0
    seed: int
    max_tokens: int = 2048
    stop_tokens: list = []
    system_prompt: str = ""
    digest: str = ""
    registered_by: str = ""


@app.get("/admin/model-registry")
def list_model_registry(_auth=Depends(_check_api_key)):
    """List all registered reference inference stacks."""
    return {"stacks": [s.to_dict() for s in model_registry.all_stacks()]}


@app.post("/admin/model-registry", status_code=201)
def register_model_stack(req: _StackRegisterRequest, _auth=Depends(_check_api_key)):
    """
    Register a reference inference stack for a model (§12.3).

    Once registered, tasks for this model will have temperature and seed
    overridden by the stack values, and will be eligible for Standard-tier
    (RedundantExecutionVerifier) verification when --verify-sample-rate > 0.
    """
    stack = ModelStack(
        model_id=req.model_id,
        runtime=req.runtime,
        format=req.format,
        temperature=req.temperature,
        seed=req.seed,
        max_tokens=req.max_tokens,
        stop_tokens=req.stop_tokens,
        system_prompt=req.system_prompt,
        digest=req.digest,
        registered_by=req.registered_by,
    )
    try:
        model_registry.register(stack)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"registered": stack.to_dict()}


@app.get("/admin/model-registry/{model_id}")
def get_model_stack(model_id: str, _auth=Depends(_check_api_key)):
    """Get the registered stack for a specific model."""
    stack = model_registry.get(model_id)
    if stack is None:
        raise HTTPException(status_code=404, detail=f"No stack registered for '{model_id}'")
    return stack.to_dict()


@app.get("/")
def root():
    return {"service": "DAI Orchestrator", "version": "0.1.0", "docs": "/docs", "status": "/status"}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DAI Orchestrator")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--api-key", default=os.getenv("DAI_API_KEY"),
                        help="Require this key on /v1/chat/completions requests (Bearer token). "
                             "Omit for open access (local dev default).")

    # On-chain mode (opt-in — mock mode is the default)
    parser.add_argument("--chain", action="store_true",
                        help="Enable on-chain mode (real DAI rewards + SlashingContract)")
    parser.add_argument("--rpc-url",
                        default=os.getenv("DAI_RPC_URL", "http://localhost:8545"),
                        help="RPC endpoint for the chain (default: localhost Hardhat node)")
    parser.add_argument("--token-contract",
                        default=os.getenv("DAI_TOKEN_CONTRACT"),
                        help="Deployed DAIToken address")
    parser.add_argument("--slashing-contract",
                        default=os.getenv("DAI_SLASHING_CONTRACT"),
                        help="Deployed SlashingContract address")
    parser.add_argument("--payment-contract",
                        default=os.getenv("DAI_PAYMENT_CONTRACT"),
                        help="Deployed PaymentContract address")
    parser.add_argument("--distributor-contract",
                        default=os.getenv("DAI_DISTRIBUTOR_CONTRACT"),
                        help="Deployed MerkleDistributor address (reward settlement)")
    parser.add_argument("--orchestrator-key",
                        default=os.getenv("DAI_ORCHESTRATOR_KEY"),
                        help="Orchestrator wallet private key (needs UPDATER_ROLE on "
                             "MerkleDistributor + ORCHESTRATOR_ROLE on SlashingContract)")
    parser.add_argument("--settle-interval", type=int,
                        default=int(os.getenv("DAI_SETTLE_INTERVAL", "3600")),
                        help="Seconds between reward-settlement epochs (publish a "
                             "cumulative Merkle root). Chain mode only. Default 3600.")

    parser.add_argument("--cold-start-allowance", type=float,
                        default=float(os.getenv("DAI_COLD_START_ALLOWANCE", "240")),
                        help="Extra seconds given to a node whose model is unloaded "
                             "(a sleeping node) to load it (default 240).")
    parser.add_argument("--task-timeout", type=float,
                        default=float(os.getenv("DAI_TASK_TIMEOUT", "60")),
                        help="Seconds to wait for a node to return a result before "
                             "issuing a 504 (default 60). Raise for CPU nodes or "
                             "large models.")

    # Verification (Standard tier — docs/VERIFICATION.md)
    parser.add_argument("--verify-sample-rate", type=float,
                        default=float(os.getenv("DAI_VERIFY_SAMPLE_RATE", "0.0")),
                        help="Fraction of tasks [0..1] silently re-run on a second "
                             "node for redundant verification. 0 = legacy non-empty "
                             "check only, no rechecks (default).")
    parser.add_argument("--verify-threshold", type=float,
                        default=float(os.getenv("DAI_VERIFY_THRESHOLD", "0.85")),
                        help="Agreement threshold [0..1] for redundant verification "
                             "(default 0.85). Only used when --verify-sample-rate > 0.")
    parser.add_argument("--embedding-url",
                        default=os.getenv("DAI_EMBEDDING_URL"),
                        help="Base URL of an OpenAI-compatible embedding endpoint for "
                             "semantic comparison (e.g. http://localhost:11434 for Ollama). "
                             "When set, replaces the SequenceMatcher placeholder with "
                             "embedding cosine similarity. Requires --verify-sample-rate > 0.")
    parser.add_argument("--embedding-model",
                        default=os.getenv("DAI_EMBEDDING_MODEL", "nomic-embed-text"),
                        help="Embedding model to use (default: nomic-embed-text). "
                             "Pull with: ollama pull nomic-embed-text")
    parser.add_argument("--registry-file",
                        default=os.getenv("DAI_REGISTRY_FILE"),
                        help="Path to a JSON file that persists the model registry "
                             "across orchestrator restarts. Loaded at startup; "
                             "subsequent registrations auto-save (VERIFICATION_PROTOCOL §12.6).")

    # Committee escalation (VERIFICATION_PROTOCOL §13). Defaults are §13.8
    # starting points; all four are testnet-calibrated.
    parser.add_argument("--committee-size", type=int,
                        default=int(os.getenv("DAI_COMMITTEE_SIZE", str(DEFAULT_COMMITTEE_SIZE))),
                        help=f"Committee size (must be odd >=1; default {DEFAULT_COMMITTEE_SIZE}).")
    parser.add_argument("--committee-timeout", type=float,
                        default=float(os.getenv("DAI_COMMITTEE_TIMEOUT", str(DEFAULT_COMMITTEE_TIMEOUT_S))),
                        help=f"Max seconds to wait for committee responses "
                             f"(default {DEFAULT_COMMITTEE_TIMEOUT_S}).")
    parser.add_argument("--appeal-window", type=float,
                        default=float(os.getenv("DAI_APPEAL_WINDOW", str(DEFAULT_APPEAL_WINDOW_S))),
                        help=f"Seconds between a dishonest verdict and the slash "
                             f"firing (default {DEFAULT_APPEAL_WINDOW_S}). 0 = immediate.")
    parser.add_argument("--slash-fraction", type=float,
                        default=float(os.getenv("DAI_SLASH_FRACTION", str(DEFAULT_SLASH_FRACTION))),
                        help=f"Fraction of unvested bond burned on a verified "
                             f"dishonesty verdict (0,1]; default {DEFAULT_SLASH_FRACTION}.")

    args = parser.parse_args()
    if args.committee_size < 1 or args.committee_size % 2 == 0:
        parser.error("--committee-size must be an odd integer >= 1")
    if not 0.0 < args.slash_fraction <= 1.0:
        parser.error("--slash-fraction must be in (0, 1]")

    if args.api_key:
        _api_key = args.api_key
        log.info("API key authentication enabled on /v1/chat/completions")
    else:
        log.info("Running in open-access mode (no API key required)")

    _task_timeout = args.task_timeout
    _cold_start_allowance = args.cold_start_allowance
    log.info(f"Task timeout: {_task_timeout}s  (+{_cold_start_allowance:.0f}s for a sleeping node)")

    verifier = make_verifier(
        args.verify_sample_rate,
        args.verify_threshold,
        embedding_url=args.embedding_url,
        embedding_model=args.embedding_model,
    )
    if args.verify_sample_rate > 0:
        comparator_desc = (
            f"semantic embedding  url={args.embedding_url}  model={args.embedding_model}"
            if args.embedding_url
            else "sequence-ratio (placeholder; use --embedding-url for semantic comparison)"
        )
        log.info(
            f"Verification: redundant execution ENABLED  "
            f"sample_rate={args.verify_sample_rate}  threshold={args.verify_threshold}  "
            f"comparator={comparator_desc}"
        )
    else:
        log.info("Verification: content-only (legacy non-empty check; no redundant rechecks)")

    if args.registry_file:
        loaded = model_registry.load_from_file(args.registry_file)
        model_registry.set_persistence(args.registry_file)
        log.info(
            f"Model registry: persisted at {args.registry_file}  "
            f"loaded={loaded} stacks  autosave=ON"
        )
    else:
        log.info("Model registry: in-memory only (no --registry-file)")

    _committee_size = args.committee_size
    _committee_timeout = args.committee_timeout
    _appeal_window = args.appeal_window
    _slash_fraction = args.slash_fraction
    log.info(
        f"Committee escalation: size={_committee_size} "
        f"timeout={_committee_timeout:.0f}s "
        f"appeal_window={_appeal_window:.0f}s "
        f"slash_fraction={_slash_fraction:.2%}"
    )

    if args.chain:
        missing = [n for n, v in [
            ("--token-contract",       args.token_contract),
            ("--slashing-contract",    args.slashing_contract),
            ("--payment-contract",     args.payment_contract),
            ("--distributor-contract", args.distributor_contract),
            ("--orchestrator-key",     args.orchestrator_key),
        ] if not v]
        if missing:
            parser.error(f"--chain requires: {', '.join(missing)}")

        from chain_ledger import ChainLedger
        chain_ledger = ChainLedger(
            rpc_url=args.rpc_url,
            orchestrator_key=args.orchestrator_key,
            token_addr=args.token_contract,
            slashing_addr=args.slashing_contract,
            payment_addr=args.payment_contract,
            distributor_addr=args.distributor_contract,
        )
        _settle_interval = args.settle_interval
        log.info(
            f"Running in ON-CHAIN mode — rewards accrue off-chain, settled "
            f"every {args.settle_interval}s via MerkleDistributor"
        )
    else:
        log.info("Running in MOCK mode — in-memory ledger only (no blockchain required)")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
