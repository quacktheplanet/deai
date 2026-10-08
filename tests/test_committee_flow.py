"""
The committee's verdict, end to end through the HTTP path, after the live run
in docs/CROSS_HARDWARE_RESULTS.md found two problems:
  - when the committee upheld the checker, the requester still got a 502;
  - the node it had just caught stayed idle-longest and drew the next request.
"""

import asyncio
import json
import time

import httpx
import pytest
from httpx import ASGITransport

import orchestrator as orc
from orchestrator import app, nodes, results, pending_events, stats, ledger, model_registry
from model_registry import ModelStack
from shared.schemas import NodeInfo, NodeStatus, TaskResult
from verification import RedundantExecutionVerifier

MODEL = "qwen3:8b"
GOOD = "Paris is the capital of France."
JUNK = "zzz junk zzz"


def _exact(a, b):
    return 1.0 if a == b else 0.0


class _WS:
    def __init__(self, node_id, text):
        self.node_id, self.text, self.got = node_id, text, 0

    async def send_text(self, data):
        msg = json.loads(data)
        if msg.get("type") != "task":
            return
        self.got += 1
        task_id = msg["payload"]["task_id"]

        async def _resolve():
            await asyncio.sleep(0.01)
            if self.node_id in nodes:
                nodes[self.node_id].status = NodeStatus.idle
            results[task_id] = TaskResult(task_id=task_id, node_id=self.node_id, content=self.text, tokens_used=5)
            if task_id in pending_events:
                pending_events[task_id].set()

        asyncio.create_task(_resolve())


def _add(node_id, text, recent=False):
    ws = _WS(node_id, text)
    nodes[node_id] = orc.NodeConnection(ws=ws, info=NodeInfo(node_id=node_id, models=[MODEL]))
    if recent:
        nodes[node_id].last_task_time = time.time()
    return ws


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    for d in (nodes, results, pending_events):
        d.clear()
    for k in stats:
        stats[k] = 0
    ledger._balances.clear()
    model_registry._stacks.clear()
    model_registry.register(ModelStack(model_id=MODEL, runtime="ollama", seed=42))
    monkeypatch.setattr(orc, "_api_key", None)
    monkeypatch.setattr(orc, "chain_ledger", None)
    monkeypatch.setattr(orc, "verifier", RedundantExecutionVerifier(sample_rate=1.0, comparator=_exact))
    slashes = []

    def no_slash(node_id, *a, **k):
        slashes.append(node_id)            # recorded when scheduled, not when it would fire
        return asyncio.sleep(0)
    monkeypatch.setattr(orc, "_schedule_slash", no_slash)
    yield slashes
    for d in (nodes, results, pending_events):
        d.clear()
    model_registry._stacks.clear()


async def _post():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post("/v1/chat/completions",
                            json={"model": MODEL, "messages": [{"role": "user", "content": "capital?"}]})


async def test_checker_upheld_returns_the_confirmed_answer_and_pays_the_checker(reset):
    _add("cheat", JUNK)                                   # idle longest: becomes primary
    for nid in ("a", "b", "c", "d"):
        _add(nid, GOOD, recent=True)
    r = await _post()
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == GOOD
    assert r.headers["X-DAI-Verification"] == "verified"
    assert reset == ["cheat"]                             # slash scheduled for the cheat only
    assert ledger.balance("cheat") == 0
    paid = [nid for nid in ("a", "b", "c", "d") if ledger.balance(nid) > 0]
    assert len(paid) == 1                                 # the checker, for the answer returned


async def test_a_caught_node_draws_no_more_requests(reset):
    cheat = _add("cheat", JUNK)
    for nid in ("a", "b", "c", "d"):
        _add(nid, GOOD, recent=True)
    await _post()
    assert orc.quarantined(nodes["cheat"])
    before = cheat.got
    for _ in range(3):
        r = await _post()
        assert r.status_code == 200 and r.headers["X-DAI-Verification"] == "verified"
    assert cheat.got == before                            # not primary, checker or committee
    assert reset == ["cheat"]


async def test_primary_upheld_quarantines_the_checker(reset):
    _add("honest", GOOD)                                  # primary
    _add("cheat", JUNK, recent=True)
    for nid in ("a", "b", "c"):
        _add(nid, GOOD, recent=True)
    nodes["cheat"].last_task_time = time.time() - 1       # next idle-longest: the checker
    r = await _post()
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == GOOD
    assert reset == ["cheat"]
    assert orc.quarantined(nodes["cheat"]) and not orc.quarantined(nodes["honest"])


async def test_both_sides_are_released_when_no_committee_can_form(reset):
    _add("p", JUNK)
    _add("c", GOOD, recent=True)                          # only two nodes: no quorum
    r = await _post()
    assert r.status_code == 200 and r.headers["X-DAI-Verification"] == "unverified"
    assert not orc.quarantined(nodes["p"]) and not orc.quarantined(nodes["c"])
    assert reset == []


def test_default_threshold_is_the_measured_one():
    assert RedundantExecutionVerifier(sample_rate=1.0).agreement_threshold == 0.75
