"""
Cheap checks (VERIFICATION_PROTOCOL §9): known-answer tasks that need one node,
not two. Qualification screens a node in before paid work; canaries keep
checking it afterwards. A miss is evidence, not proof, so it never slashes: a
missing node gets its paid work rechecked, and only repeated misses in a row
stop it getting work.
"""

import asyncio
import json
import random

import httpx
import pytest
from httpx import ASGITransport

import orchestrator as orc
from orchestrator import app, nodes, results, pending_events, stats, ledger, model_registry
from golden import GoldenEntry, GoldenSet
import golden as golden_mod
from model_registry import ModelStack
from shared.schemas import NodeInfo, NodeStatus, TaskResult
from verification import ComparatorUnavailable, RedundantExecutionVerifier

MODEL = "qwen3:8b"
REFS = {f"q{i}": f"Reference answer number {i} about topic {i}." for i in range(6)}


def _golden() -> GoldenSet:
    return GoldenSet([GoldenEntry(model_id=MODEL, messages=[{"role": "user", "content": q}],
                                  reference=a, seed=42) for q, a in REFS.items()])


def _exact(a: str, b: str) -> float:
    return 1.0 if a.strip() == b.strip() else 0.0


class _WS:
    """An honest node answers golden prompts with the reference and anything else
    with `answer`; a cheater answers everything with junk."""

    def __init__(self, node_id, honest=True, answer="A real answer.", delay=0.01):
        self.node_id, self.honest, self.answer, self.delay = node_id, honest, answer, delay
        self.prompts = []

    async def send_text(self, data):
        msg = json.loads(data)
        if msg.get("type") != "task":
            return
        p = msg["payload"]
        prompt = p["messages"][-1]["content"]
        self.prompts.append(prompt)
        text = (REFS.get(prompt, self.answer) if self.honest else "cheap junk from a tiny model")

        async def _resolve():
            await asyncio.sleep(self.delay)
            if self.node_id in nodes:
                nodes[self.node_id].status = NodeStatus.idle
            results[p["task_id"]] = TaskResult(task_id=p["task_id"], node_id=self.node_id,
                                               content=text, tokens_used=10)
            if p["task_id"] in pending_events:
                pending_events[p["task_id"]].set()

        asyncio.create_task(_resolve())


def _add(node_id, **kw):
    ws = _WS(node_id, **kw)
    nodes[node_id] = orc.NodeConnection(ws=ws, info=NodeInfo(node_id=node_id, models=[MODEL]))
    return nodes[node_id], ws


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    for d in (nodes, results, pending_events):
        d.clear()
    for k in stats:
        stats[k] = 0
    ledger._balances.clear()
    model_registry._stacks.clear()
    monkeypatch.setattr(orc, "_api_key", None)
    monkeypatch.setattr(orc, "chain_ledger", None)
    monkeypatch.setattr(orc, "golden", _golden())
    monkeypatch.setattr(orc, "_qualify_challenges", 3)
    monkeypatch.setattr(orc, "_canary_max_fails", 3)
    monkeypatch.setattr(orc, "verifier", RedundantExecutionVerifier(sample_rate=0.0001, comparator=_exact,
                                                                    rng=random.Random(1)))
    yield
    for d in (nodes, results, pending_events):
        d.clear()
    model_registry._stacks.clear()


async def _post():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post("/v1/chat/completions",
                            json={"model": MODEL, "messages": [{"role": "user", "content": "hello"}]})


# ── The golden set ────────────────────────────────────────────────────────────

def test_golden_set_round_trips_and_samples(tmp_path):
    gs = _golden()
    gs.save(tmp_path / "g.json")
    back = GoldenSet.load(tmp_path / "g.json")
    assert back.models() == {MODEL} and len(back.for_model(MODEL)) == 6
    assert len(back.sample(MODEL, 3, random.Random(0))) == 3
    assert back.sample("other", 3) == []


def test_build_stores_reference_answers(tmp_path, monkeypatch):
    def fake_post(url, timeout, json):
        assert json["temperature"] == 0.0 and json["seed"] == 42
        content = "" if "skip" in json["messages"][0]["content"] else "ref:" + json["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", fake_post)
    out = tmp_path / "g.json"
    golden_mod.build(MODEL, "http://x", ["one", "skip me", "two"], 42, 256, str(out))
    gs = GoldenSet.load(out)
    assert [e.reference for e in gs.entries] == ["ref:one", "ref:two"]
    assert all(e.seed == 42 and e.max_tokens == 256 for e in gs.entries)


# ── Qualification ─────────────────────────────────────────────────────────────

async def test_honest_node_qualifies_and_gets_a_measured_speed():
    node, ws = _add("honest")
    await orc._qualify(node)
    assert node.qualified == {MODEL: True}
    assert node.measured_tps and node.measured_tps > 0
    assert len(ws.prompts) == 3 and all(p in REFS for p in ws.prompts)


async def test_cheating_node_fails_and_gets_no_work_for_that_model():
    node, _ = _add("cheat", honest=False)
    await orc._qualify(node)
    assert node.qualified == {MODEL: False}
    r = await _post()
    assert r.status_code == 503


async def test_no_work_while_qualification_is_pending():
    node, _ = _add("new")
    node.qualified = {MODEL: None}
    assert not orc.cleared_for(node, MODEL)
    assert orc.cleared_for(node, "any")


async def test_models_outside_the_golden_set_need_no_qualification():
    node, _ = _add("n")
    assert orc.cleared_for(node, "llama3")


async def test_qualification_off_by_default(monkeypatch):
    monkeypatch.setattr(orc, "_qualify_challenges", 0)
    node, _ = _add("n")
    assert orc.cleared_for(node, MODEL)


async def test_comparator_outage_during_qualification_does_not_fail_the_node(monkeypatch):
    def down(a, b):
        raise ComparatorUnavailable("loading")
    monkeypatch.setattr(orc, "verifier", RedundantExecutionVerifier(sample_rate=0.0001, comparator=down))
    node, _ = _add("n", honest=False)
    await orc._qualify(node)
    assert node.qualified == {MODEL: True}   # can't judge: accepted, as with any unverified check


# ── Canaries ──────────────────────────────────────────────────────────────────

async def test_canary_match_is_paid_like_work():
    node, ws = _add("honest")
    node.qualified = {MODEL: True}
    assert await orc.send_canary(random.Random(0)) is True
    assert node.canary_passed == 1 and stats["canaries_passed"] == 1
    assert ledger.balance("honest") > 0
    assert ws.prompts[0] in REFS


async def test_canary_miss_makes_the_node_a_suspect_whose_work_is_rechecked():
    import time
    model_registry.register(ModelStack(model_id=MODEL, runtime="ollama", seed=42))
    cheat, _ = _add("cheat", honest=False)
    _, other_ws = _add("other")
    cheat.qualified = {MODEL: True}
    nodes["other"].qualified = {MODEL: True}
    nodes["other"].status = NodeStatus.busy          # the canary must land on the cheat
    assert await orc.send_canary(random.Random(0)) is False
    nodes["other"].status = NodeStatus.idle
    assert orc.is_suspect(cheat) and cheat.canary_failed == 1
    nodes["other"].last_task_time = time.time()      # so the cheat is picked as primary
    other_ws.prompts.clear()
    r = await _post()
    # Sampling is ~0 (rate 0.0001), so only the suspect rule can have sent the
    # task to a checker. The answers disagree and there's no committee quorum,
    # so it is accepted unverified - but it WAS rechecked.
    assert other_ws.prompts == ["hello"]
    assert r.status_code == 200 and r.headers["X-DAI-Verification"] == "unverified"


async def test_a_passed_canary_clears_suspicion():
    node, ws = _add("n")
    node.qualified = {MODEL: True}
    node.canary_streak = 2
    assert await orc.send_canary(random.Random(0)) is True
    assert not orc.is_suspect(node)


async def test_repeated_misses_stop_routing_but_never_slash(monkeypatch):
    slashes = []
    monkeypatch.setattr(orc, "_apply_slash", lambda *a, **k: slashes.append(a))
    node, _ = _add("cheat", honest=False)
    node.qualified = {MODEL: True}
    for _ in range(3):
        assert await orc.send_canary(random.Random(0)) is False
    assert node.excluded and not slashes
    assert not orc.cleared_for(node, MODEL)
    assert await orc.send_canary(random.Random(0)) is None   # no longer a candidate
    r = await _post()
    assert r.status_code == 503


async def test_canary_with_comparator_down_is_inconclusive(monkeypatch):
    def down(a, b):
        raise ComparatorUnavailable("loading")
    monkeypatch.setattr(orc, "verifier", RedundantExecutionVerifier(sample_rate=0.0001, comparator=down))
    node, _ = _add("cheat", honest=False)
    node.qualified = {MODEL: True}
    assert await orc.send_canary(random.Random(0)) is None
    assert node.canary_failed == 0 and not orc.is_suspect(node)


async def test_no_canary_without_an_idle_cleared_node():
    node, _ = _add("busy")
    node.qualified = {MODEL: True}
    node.status = NodeStatus.busy
    assert await orc.send_canary() is None


def test_status_reports_the_checks():
    from starlette.testclient import TestClient
    node, _ = _add("n")
    node.qualified = {MODEL: True}
    node.measured_tps = 42.04
    node.canary_failed, node.canary_streak = 1, 1
    with TestClient(app) as client:
        row = client.get("/status").json()["nodes"][0]
    assert row["qualified"] == {MODEL: True}
    assert row["measured_tokens_per_s"] == 42.0
    assert row["canaries"] == {"passed": 0, "failed": 1, "suspect": True, "excluded": False}
