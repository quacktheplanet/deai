"""
Reproducible answers: a llama-server node asks for no prompt-cache reuse on
seeded requests, and identical answers verify by exact match without the
embedding comparator.
"""

import json

import httpx

import compute.node as node_mod


def _mock(monkeypatch, seen, props=None):
    def handler(request):
        if request.url.path == "/props":
            return httpx.Response(200, json=props) if props else httpx.Response(404)
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Paris."}}],
                                         "usage": {"completion_tokens": 2}})
    real = httpx.AsyncClient
    monkeypatch.setattr(node_mod.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


async def test_seeded_requests_carry_the_extra_fields(monkeypatch):
    seen = []
    _mock(monkeypatch, seen)
    monkeypatch.setattr(node_mod, "SEEDED_REQUEST_EXTRA", {"cache_prompt": False})
    await node_mod.run_ollama_inference("m", [{"role": "user", "content": "q"}], 8, 0.0,
                                        "http://backend", ["m"], seed=42)
    await node_mod.run_ollama_inference("m", [{"role": "user", "content": "q"}], 8, 0.7,
                                        "http://backend", ["m"], seed=None)
    assert seen[0]["seed"] == 42 and seen[0]["cache_prompt"] is False
    assert "cache_prompt" not in seen[1]          # unseeded work isn't meant to repeat


async def test_other_backends_get_nothing_extra(monkeypatch):
    seen = []
    _mock(monkeypatch, seen)
    monkeypatch.setattr(node_mod, "SEEDED_REQUEST_EXTRA", {})
    await node_mod.run_ollama_inference("m", [{"role": "user", "content": "q"}], 8, 0.0,
                                        "http://backend", ["m"], seed=42)
    assert "cache_prompt" not in seen[0]


async def test_slot_count_comes_from_props(monkeypatch):
    _mock(monkeypatch, [], props={"total_slots": 4})
    assert await node_mod.llama_server_slots("http://backend") == 4
    _mock(monkeypatch, [], props=None)
    assert await node_mod.llama_server_slots("http://backend") is None
