"""
Likelihood check: "did this text come from the claimed model?" — the question
the embedding comparator can't answer (a cheaper model's answer means the same).
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
from likelihood import LikelihoodPolicy, LikelihoodScore, gbnf_literal, summarize
import likelihood as lik_mod

MODEL = "qwen2.5:7b"
HONEST = "The TCP handshake is SYN, SYN-ACK, ACK: the client asks, the server agrees, the client confirms."
CHEAP = "TCP handshake: SYN then ACK then you are connected and it is done, that is all of it really."


# ── The module ────────────────────────────────────────────────────────────────

def test_grammar_admits_exactly_the_text_with_escapes():
    g = gbnf_literal('a "quote"\\ and\nnew line\ttab')
    assert g == 'root ::= "a \\"quote\\"\\\\ and\\nnew line\\ttab"'


def test_summarize_counts_first_choices_and_mean_logprob():
    probs = [{"token": "a", "logprob": -0.1, "top_logprobs": [{"token": "a", "logprob": -0.1}]},
             {"token": "b", "logprob": -2.0, "top_logprobs": [{"token": "c", "logprob": -0.3}]}]
    s = summarize(probs)
    assert (s.tokens, s.top1_rate) == (2, 0.5)
    assert s.mean_logprob == pytest.approx(-1.05)
    assert summarize([]) is None


def test_policy_judges_and_leaves_short_text_undecided():
    p = LikelihoodPolicy(min_tokens=16, min_top1=0.85, min_mean_logprob=-0.6)
    assert p.judge(LikelihoodScore(60, 0.95, -0.18)) is True
    assert p.judge(LikelihoodScore(250, 0.70, -1.5)) is False
    assert p.judge(LikelihoodScore(60, 0.95, -0.9)) is False
    assert p.judge(LikelihoodScore(3, 0.30, -7.0)) is None
    assert p.judge(None) is None


async def test_score_text_forces_the_text_and_reads_probabilities(monkeypatch):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "<|user|>q<|assistant|>"})
        return httpx.Response(200, json={"content": "hi there", "completion_probabilities": [
            {"token": "hi", "logprob": -0.2, "top_logprobs": [{"token": "hi", "logprob": -0.2}]},
            {"token": " there", "logprob": -0.4, "top_logprobs": [{"token": " there", "logprob": -0.4}]}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(lik_mod.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    s = await lik_mod.score_text("http://backend", [{"role": "user", "content": "q"}], "hi there")
    assert s.tokens == 2 and s.top1_rate == 1.0
    body = seen[1][1]
    assert body["grammar"] == 'root ::= "hi there"' and body["n_probs"] == 1
    assert body["post_sampling_probs"] is False and body["temperature"] == 0.0


# ── The orchestrator path ─────────────────────────────────────────────────────

class _WS:
    """Generates `text` for tasks; scores any text as honest/cheap by content."""

    def __init__(self, node_id, text, scores=True):
        self.node_id, self.text, self.scores = node_id, text, scores
        self.tasks, self.scored = 0, 0

    async def send_text(self, data):
        msg = json.loads(data)
        p = msg.get("payload", {})
        if msg.get("type") == "task":
            self.tasks += 1

            async def _done():
                await asyncio.sleep(0.01)
                nodes[self.node_id].status = NodeStatus.idle
                results[p["task_id"]] = TaskResult(task_id=p["task_id"], node_id=self.node_id,
                                                   content=self.text, tokens_used=20)
                if p["task_id"] in pending_events:
                    pending_events[p["task_id"]].set()
            asyncio.create_task(_done())
        elif msg.get("type") == "score":
            self.scored += 1
            honest = p["text"] == HONEST

            async def _scored():
                await asyncio.sleep(0.01)
                nodes[self.node_id].status = NodeStatus.idle
                orc.score_results[p["task_id"]] = {
                    "task_id": p["task_id"], "tokens": 60,
                    "top1_rate": 0.96 if honest else 0.62, "mean_logprob": -0.15 if honest else -1.6}
                if p["task_id"] in pending_events:
                    pending_events[p["task_id"]].set()
            asyncio.create_task(_scored())


def _add(node_id, text, can_score=True, recent=False):
    ws = _WS(node_id, text)
    nodes[node_id] = orc.NodeConnection(ws=ws, info=NodeInfo(node_id=node_id, models=[MODEL], can_score=can_score))
    if recent:
        nodes[node_id].last_task_time = time.time()
    return ws


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    for d in (nodes, results, pending_events, orc.score_results):
        d.clear()
    for k in stats:
        stats[k] = 0
    ledger._balances.clear()
    model_registry._stacks.clear()
    model_registry.register(ModelStack(model_id=MODEL, runtime="llama.cpp", seed=42))
    monkeypatch.setattr(orc, "_api_key", None)
    monkeypatch.setattr(orc, "chain_ledger", None)
    monkeypatch.setattr(orc, "_likelihood", LikelihoodPolicy())
    monkeypatch.setattr(orc, "verifier", RedundantExecutionVerifier(sample_rate=1.0, comparator=lambda a, b: 1.0))
    slashes = []

    def no_slash(node_id, *a, **k):
        slashes.append(node_id)
        return asyncio.sleep(0)
    monkeypatch.setattr(orc, "_schedule_slash", no_slash)
    yield slashes
    for d in (nodes, results, pending_events, orc.score_results):
        d.clear()
    model_registry._stacks.clear()


async def _post():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post("/v1/chat/completions",
                            json={"model": MODEL, "messages": [{"role": "user", "content": "TCP?"}]})


async def test_likely_text_is_verified_by_one_score_without_regenerating(reset):
    primary = _add("p", HONEST)
    others = [_add(n, HONEST, recent=True) for n in ("a", "b")]
    r = await _post()
    assert r.status_code == 200 and r.headers["X-DAI-Verification"] == "verified"
    assert sum(w.scored for w in others) == 1
    assert sum(w.tasks for w in others) == 0          # no second generation
    assert primary.tasks == 1 and not reset


async def test_substitute_is_convicted_by_the_panel_and_replaced(reset):
    _add("cheat", CHEAP)
    others = [_add(n, HONEST, recent=True) for n in ("a", "b", "c", "d", "e")]
    r = await _post()
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == HONEST
    assert reset == ["cheat"] and orc.quarantined(nodes["cheat"])
    assert ledger.balance("cheat") == 0
    assert sum(w.scored for w in others) == 1 + 3      # the first scorer, then a panel of three


async def test_no_scorers_falls_back_to_generate_and_compare(reset):
    _add("p", HONEST)
    checker = _add("c", HONEST, can_score=False, recent=True)
    r = await _post()
    assert r.status_code == 200 and r.headers["X-DAI-Verification"] == "verified"
    assert checker.tasks == 1 and checker.scored == 0


async def test_an_objection_without_a_panel_falls_back(reset):
    _add("cheat", CHEAP)
    scorer = _add("a", HONEST, recent=True)            # objects, but no one else to ask
    r = await _post()
    assert r.status_code == 200
    assert scorer.scored == 1 and scorer.tasks == 1     # then the old check ran on it
    assert not reset


async def test_off_by_default(reset, monkeypatch):
    monkeypatch.setattr(orc, "_likelihood", None)
    _add("p", HONEST)
    other = _add("a", HONEST, recent=True)
    await _post()
    assert other.scored == 0 and other.tasks == 1
