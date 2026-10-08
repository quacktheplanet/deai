"""
Sleeping nodes: an idle node may unload its model to give the GPU back to its
owner, stay connected, and say so. The orchestrator prefers warm nodes and,
when it must use a sleeping one, waits long enough for the model to load.
"""

import asyncio
import json

import httpx
import pytest
from httpx import ASGITransport

import orchestrator as orc
from orchestrator import app, nodes, results, pending_events
from shared.schemas import NodeInfo, NodeStatus, TaskResult
import compute.node as node_mod
from compute.node import Warmth, KEEP_WARM_INTERVAL

MODEL = "qwen3:8b"


# ── The node's idle logic ─────────────────────────────────────────────────────

def test_idle_node_pokes_its_model_to_keep_it_loaded():
    w = Warmth(True, None, can_unload=True, now=0)
    assert w.next_action(KEEP_WARM_INTERVAL - 1) is None
    assert w.next_action(KEEP_WARM_INTERVAL) == "poke"


def test_node_sleeps_after_the_idle_time():
    w = Warmth(True, 600, can_unload=True, now=0)
    w.last_poke = 590            # pokes don't count as activity
    assert w.next_action(599) is None
    assert w.next_action(600) == "sleep"


def test_a_task_resets_the_sleep_timer():
    w = Warmth(True, 600, can_unload=True, now=0)
    w.task_started(500)
    assert w.next_action(700) != "sleep"
    assert w.next_action(1100) == "sleep"


def test_asleep_node_does_nothing_until_a_task_wakes_it():
    w = Warmth(True, 600, can_unload=True, now=0)
    w.warm = False
    assert w.next_action(10_000) is None
    assert w.task_finished(10_050) is True       # news: tell the orchestrator
    assert w.warm
    assert w.task_finished(10_060) is False      # already warm: nothing to say


def test_backends_that_cannot_unload_never_sleep_or_poke():
    w = Warmth(True, 600, can_unload=False, now=0)
    assert w.sleep_after is None
    assert w.next_action(1e9) is None


def test_unmanaged_cold_node_keeps_reporting_cold():
    """--no-warmup on Ollama: the model may be unloaded at any time, so the node
    never claims to be warm."""
    w = Warmth(False, None, can_unload=False, now=0)
    assert w.task_finished(5) is False
    assert not w.warm


async def test_unload_asks_ollama_to_drop_the_model(monkeypatch):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={})

    real = httpx.AsyncClient
    monkeypatch.setattr(node_mod.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert await node_mod.unload_model("qwen2.5", "http://ollama", ["qwen2.5:14b"])
    assert sent == [{"model": "qwen2.5:14b", "keep_alive": 0}]


# ── The orchestrator's side ───────────────────────────────────────────────────

class _WS:
    """Answers after `delay` seconds."""

    def __init__(self, node_id, delay):
        self.node_id, self.delay, self.got = node_id, delay, []

    async def send_text(self, data):
        msg = json.loads(data)
        if msg.get("type") != "task":
            return
        task_id = msg["payload"]["task_id"]
        self.got.append(task_id)

        async def _resolve():
            await asyncio.sleep(self.delay)
            if self.node_id in nodes:
                nodes[self.node_id].status = NodeStatus.idle
            results[task_id] = TaskResult(task_id=task_id, node_id=self.node_id,
                                          content="The answer.", tokens_used=2)
            if task_id in pending_events:
                pending_events[task_id].set()

        asyncio.create_task(_resolve())


def _add(node_id, warm, delay=0.01):
    ws = _WS(node_id, delay)
    nodes[node_id] = orc.NodeConnection(ws=ws, info=NodeInfo(node_id=node_id, models=[MODEL], warm=warm))
    return ws


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    for d in (nodes, results, pending_events):
        d.clear()
    monkeypatch.setattr(orc, "_api_key", None)
    monkeypatch.setattr(orc, "chain_ledger", None)
    yield
    for d in (nodes, results, pending_events):
        d.clear()


async def _post():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post("/v1/chat/completions",
                            json={"model": MODEL, "messages": [{"role": "user", "content": "q"}]})


async def test_requests_go_to_the_warm_node():
    asleep = _add("asleep", warm=False)
    awake = _add("awake", warm=True)
    import time
    nodes["awake"].last_task_time = time.time()   # even when the warm one worked most recently
    r = await _post()
    assert r.status_code == 200
    assert awake.got and not asleep.got


async def test_a_sleeping_node_gets_time_to_load(monkeypatch):
    monkeypatch.setattr(orc, "_task_timeout", 0.05)
    monkeypatch.setattr(orc, "_cold_start_allowance", 1.0)
    _add("asleep", warm=False, delay=0.3)    # slower than the plain timeout
    r = await _post()
    assert r.status_code == 200


async def test_a_warm_node_gets_no_extra_time(monkeypatch):
    monkeypatch.setattr(orc, "_task_timeout", 0.05)
    monkeypatch.setattr(orc, "_cold_start_allowance", 1.0)
    _add("awake", warm=True, delay=0.3)
    r = await _post()
    assert r.status_code == 504


def test_status_messages_update_warmth_and_status_shows_it():
    from starlette.testclient import TestClient
    with TestClient(app) as client:
        with client.websocket_connect("/ws/node") as ws:
            ws.send_text(json.dumps({"type": "register",
                                     "payload": {"node_id": "n1", "models": [MODEL], "warm": True}}))
            assert json.loads(ws.receive_text())["type"] == "ack"
            ws.send_text(json.dumps({"type": "status", "payload": {"node_id": "n1", "warm": False}}))
            ws.send_text(json.dumps({"type": "heartbeat", "payload": {"node_id": "n1"}}))
            for _ in range(50):
                if not nodes["n1"].info.warm:
                    break
                import time
                time.sleep(0.01)
            assert nodes["n1"].info.warm is False
            assert client.get("/status").json()["nodes"][0]["warm"] is False
