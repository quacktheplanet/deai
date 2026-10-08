"""
Verification that fails safe when its own machinery is slow or down.

Found by running two real Ollama nodes (qwen2.5:14b) against the orchestrator:
  - the embedding comparator's 10 s timeout fired while Ollama was still
    loading nomic-embed-text, it fell back to the sequence ratio, and an
    honest paraphrase (embedding cosine 0.986) scored 0.307 → MISMATCH →
    committee, which can slash an honest node;
  - a cold 14B load (72 s) on a node's first task exceeded the 60 s task
    timeout;
  - the agreement test "passed" 15/15 while nothing was compared, because the
    model had no registered stack.
"""

import asyncio

import httpx
import pytest
from httpx import ASGITransport

import orchestrator as orc
from orchestrator import app, nodes, results, pending_events, stats, ledger, model_registry
from model_registry import ModelStack
from shared.schemas import NodeInfo, NodeStatus, Task, TaskResult
from verification import (
    ComparatorUnavailable, EmbeddingComparator, RedundantExecutionVerifier,
)
import compute.node as node_mod

MODEL = "qwen3:8b"
TASK = Task(model=MODEL, messages=[{"role": "user", "content": "q"}])
SAME = "Git rebase rewrites history onto a new base; merge keeps both histories with a merge commit."
PARAPHRASE = "Merging joins two branches with a new commit, while rebasing replays your commits on top of another branch."


def _r(content: str, node: str = "n") -> TaskResult:
    return TaskResult(task_id="t", node_id=node, content=content, tokens_used=3)


def _down(a: str, b: str) -> float:
    raise ComparatorUnavailable("ConnectTimeout: embedding model still loading")


# ── Verifier: an unavailable comparator is never a mismatch ──────────────────

def test_unavailable_comparator_on_paraphrase_is_unverified_not_mismatch():
    v = RedundantExecutionVerifier(sample_rate=1.0, comparator=_down)
    out = v.compare(TASK, _r(SAME), _r(PARAPHRASE))
    assert out.accepted and out.unverified
    assert not out.escalation_required
    assert out.method == "unverified"


def test_identical_text_is_confirmed_without_the_comparator():
    v = RedundantExecutionVerifier(sample_rate=1.0, comparator=_down)
    out = v.compare(TASK, _r(SAME), _r(SAME + "\n"))
    assert out.accepted and not out.unverified
    assert out.method == "redundant_match" and out.detail == "identical text"


def test_unavailable_comparator_still_confirms_near_identical_text():
    v = RedundantExecutionVerifier(sample_rate=1.0, comparator=_down)
    out = v.compare(TASK, _r(SAME), _r(SAME.rstrip(".") + "!"))
    assert out.accepted and not out.unverified
    assert out.method == "redundant_match"
    assert "sequence ratio" in out.detail


def test_working_comparator_still_reports_mismatch():
    v = RedundantExecutionVerifier(sample_rate=1.0, comparator=lambda a, b: 0.2)
    out = v.compare(TASK, _r(SAME), _r("garbage"))
    assert not out.accepted and out.escalation_required and not out.unverified


# ── EmbeddingComparator: cold timeouts, retry, no silent fallback ────────────

class _FakePost:
    """Stands in for httpx.post: replays a script of exceptions / vectors and
    records the timeout of every call."""

    def __init__(self, script):
        self.script = list(script)
        self.timeouts = []

    def __call__(self, url, json, timeout):
        self.timeouts.append(timeout)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        req = httpx.Request("POST", url)
        data = [{"index": i, "embedding": step[i]} for i in range(len(json["input"]))]
        return httpx.Response(200, json={"data": data}, request=req)


def test_first_call_uses_cold_timeout_then_normal(monkeypatch):
    fake = _FakePost([[[1, 0], [1, 0]], [[1, 0], [0, 1]]])
    monkeypatch.setattr(httpx, "post", fake)
    c = EmbeddingComparator("http://x", timeout=10, cold_timeout=120)
    assert c("a", "b") == pytest.approx(1.0)
    assert c("a", "b") == pytest.approx(0.0)
    assert fake.timeouts == [120, 10]


def test_timeout_when_warm_retries_once_cold(monkeypatch):
    fake = _FakePost([[[1]], httpx.ReadTimeout("slow"), [[1, 0], [1, 0]]])
    monkeypatch.setattr(httpx, "post", fake)
    c = EmbeddingComparator("http://x", timeout=10, cold_timeout=120)
    assert c.warm_up()
    assert c("a", "b") == pytest.approx(1.0)
    assert fake.timeouts == [120, 10, 120]


def test_failure_raises_unavailable_instead_of_falling_back(monkeypatch):
    fake = _FakePost([httpx.ConnectError("refused")])
    monkeypatch.setattr(httpx, "post", fake)
    c = EmbeddingComparator("http://x")
    with pytest.raises(ComparatorUnavailable):
        c(SAME, SAME)


def test_warm_up_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(httpx, "post", _FakePost([httpx.ConnectError("refused")]))
    assert EmbeddingComparator("http://x").warm_up() is False


# ── Orchestrator: header + stats say what really happened ────────────────────

class _WS:
    def __init__(self, node_id: str, response: str):
        self._node_id, self._response = node_id, response

    async def send_text(self, data: str):
        import json
        msg = json.loads(data)
        if msg.get("type") != "task":
            return
        task_id = msg["payload"]["task_id"]

        async def _resolve():
            await asyncio.sleep(0.01)
            if self._node_id in nodes:
                nodes[self._node_id].status = NodeStatus.idle
            results[task_id] = TaskResult(task_id=task_id, node_id=self._node_id,
                                          content=self._response, tokens_used=3)
            if task_id in pending_events:
                pending_events[task_id].set()

        asyncio.create_task(_resolve())


def _add(node_id: str, response: str):
    nodes[node_id] = orc.NodeConnection(ws=_WS(node_id, response),
                                        info=NodeInfo(node_id=node_id, models=[MODEL]))


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
    monkeypatch.setattr(orc, "_appeal_window", 3600.0)
    yield
    for d in (nodes, results, pending_events):
        d.clear()
    model_registry._stacks.clear()


def _register():
    model_registry.register(ModelStack(model_id=MODEL, runtime="ollama", seed=42))


async def _post(verifier):
    orc.verifier = verifier
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return await c.post("/v1/chat/completions",
                                json={"model": MODEL, "messages": [{"role": "user", "content": "q"}]})
    finally:
        orc.verifier = orc.ContentVerifier()


async def test_header_verified_when_two_nodes_agree():
    _register()
    _add("a", SAME)
    _add("b", SAME)
    r = await _post(RedundantExecutionVerifier(sample_rate=1.0))
    assert r.status_code == 200
    assert r.headers["X-DAI-Verification"] == "verified"
    assert stats["verified"] == 1 and stats["unverified"] == 0


async def test_header_unchecked_without_registered_stack():
    _add("a", SAME)
    _add("b", SAME)
    r = await _post(RedundantExecutionVerifier(sample_rate=1.0))
    assert r.status_code == 200
    assert r.headers["X-DAI-Verification"] == "unchecked"
    assert stats["unchecked"] == 1


async def test_comparator_down_is_paid_unverified_without_committee(monkeypatch):
    _register()
    for nid, text in [("a", SAME), ("b", PARAPHRASE), ("c", SAME), ("d", SAME)]:
        _add(nid, text)
    convened = []
    monkeypatch.setattr(orc, "_convene_committee", lambda *a, **k: convened.append(a))
    r = await _post(RedundantExecutionVerifier(sample_rate=1.0, comparator=_down))
    assert r.status_code == 200
    assert r.headers["X-DAI-Verification"] == "unverified"
    assert stats["unverified"] == 1 and stats["completed"] == 1
    assert not convened
    assert ledger.balance("a") > 0


async def test_comparator_dying_mid_committee_falls_back_without_slash(monkeypatch):
    _register()
    for nid, text in [("a", SAME), ("b", PARAPHRASE), ("c", SAME), ("d", SAME), ("e", SAME)]:
        _add(nid, text)
    calls = []

    def flaky(a, b):
        calls.append(1)
        if len(calls) == 1:
            return 0.1          # the primary/checker comparison: mismatch
        raise ComparatorUnavailable("went away")

    slashes = []
    monkeypatch.setattr(orc, "_schedule_slash", lambda *a, **k: slashes.append(a))
    r = await _post(RedundantExecutionVerifier(sample_rate=1.0, comparator=flaky))
    assert r.status_code == 200
    assert r.headers["X-DAI-Verification"] == "unverified"
    assert not slashes


async def test_compare_runs_off_the_event_loop():
    """A slow comparator must not freeze other requests (heartbeats, results)."""
    import threading
    _register()
    _add("a", SAME)
    _add("b", PARAPHRASE)                # identical text wouldn't reach the comparator
    seen = []

    def where(a, b):
        seen.append(threading.current_thread() is threading.main_thread())
        return 1.0

    r = await _post(RedundantExecutionVerifier(sample_rate=1.0, comparator=where))
    assert r.status_code == 200
    assert seen == [False]


# ── Node: warm-up before joining ──────────────────────────────────────────────

def test_warm_target_is_first_chat_model():
    assert node_mod.warm_target(["qwen2.5:14b", "llama3"], []) == "qwen2.5:14b"
    assert node_mod.warm_target(["nomic-embed-text:latest", "qwen3:8b"], []) == "qwen3:8b"
    assert node_mod.warm_target(["any"], ["nomic-embed-text", "qwen3:8b"]) == "qwen3:8b"
    assert node_mod.warm_target(["any"], []) is None


def _capture_client(monkeypatch, status=200):
    sent = []

    def handler(request):
        sent.append((request.url.path, request.content))
        return httpx.Response(status, json={})

    real = httpx.AsyncClient
    monkeypatch.setattr(node_mod.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return sent


async def test_warm_model_on_ollama_loads_without_generating(monkeypatch):
    sent = _capture_client(monkeypatch)
    assert await node_mod.warm_model("qwen2.5", "http://ollama", ["qwen2.5:14b"])
    path, body = sent[0]
    assert path == "/api/generate"
    assert b'"prompt": ""' in body or b'"prompt":""' in body
    assert b"qwen2.5:14b" in body


async def test_warm_model_on_other_backends_asks_for_one_token(monkeypatch):
    sent = _capture_client(monkeypatch)
    assert await node_mod.warm_model("my-model", "http://llama-server", [])
    path, body = sent[0]
    assert path == "/v1/chat/completions"
    assert b'"max_tokens": 1' in body or b'"max_tokens":1' in body


async def test_warm_model_failure_does_not_raise(monkeypatch):
    _capture_client(monkeypatch, status=500)
    assert await node_mod.warm_model("qwen2.5", "http://ollama", ["qwen2.5:14b"]) is False
