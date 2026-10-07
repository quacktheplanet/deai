"""
DAI Node Client
----------------
What a miner runs to join the network.

The node:
  1. Connects to the orchestrator over WebSocket
  2. Registers its capabilities (models, hardware)
  3. Waits for task assignments
  4. Executes inference — real via Ollama, or mock for testing
  5. Returns results and waits for the next task
  6. Sends heartbeats to stay registered

Run (mock mode — no model needed):
    python compute/node.py

Run (real inference via Ollama):
    python compute/node.py --ollama
    python compute/node.py --ollama --models qwen3:8b
    python compute/node.py --ollama --gpu --vram 8

Ollama auto-detect (reads your installed models automatically):
    python compute/node.py --ollama --auto

With real inference the node loads its model before joining (a cold model can
take longer to load than the orchestrator waits for a task) and, on Ollama,
keeps it loaded while idle. --no-warmup turns both off.
"""

import asyncio
import hashlib
import json
import logging
import random
import socket
import time
import uuid
import argparse
import os
import sys

import re
import httpx
import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.schemas import NodeInfo, WSMessage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("node")

HEARTBEAT_INTERVAL = 15  # seconds

# Ollama unloads an idle model after 5 minutes by default; reloading a large
# one can take longer than the orchestrator's task timeout. An idle node pokes
# its model a little more often than that to keep it resident.
KEEP_WARM_INTERVAL = 240  # seconds


# ── Ollama helpers ────────────────────────────────────────────────────────────

async def ollama_list_models(ollama_url: str) -> list[str]:
    """Query Ollama for locally installed models."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
    except Exception as e:
        log.error(f"Could not reach Ollama at {ollama_url}: {e}")
        return []


async def ollama_resolve_model(requested: str, available: list[str]) -> str:
    """
    Match a requested model name to an available Ollama model.
    Handles shorthand: 'qwen3' matches 'qwen3:8b', 'any' uses the first available.
    """
    if requested == "any":
        # Embedding models don't support chat completions — skip them.
        chat_models = [m for m in available if "embed" not in m.lower()]
        return chat_models[0] if chat_models else (available[0] if available else "any")

    # No model list (a non-Ollama backend that serves the loaded model whatever
    # name is sent) — trust the requested name as-is.
    if not available:
        return requested

    # Exact match first
    if requested in available:
        return requested

    # Prefix match — 'qwen3' matches 'qwen3:8b'
    for name in available:
        if name.startswith(requested.split(":")[0]):
            return name

    # Fall back to first available
    log.warning(f"Model '{requested}' not found in Ollama. Using '{available[0]}' instead.")
    return available[0]


# ── Warm-up ───────────────────────────────────────────────────────────────────

async def warm_model(model: str, ollama_url: str, available_models: list[str], timeout: float = 600.0) -> bool:
    """
    Load `model` into memory before taking tasks: the first request to a cold
    model pays the whole load (over a minute for a 14B model), which used to
    land on a real task and exceed the orchestrator's task timeout.

    Ollama (its model list is known): an empty /api/generate prompt loads the
    model without generating anything, so this is also cheap enough to repeat
    as a keep-alive. Other OpenAI-compatible backends: a one-token completion.
    """
    resolved = await ollama_resolve_model(model, available_models)
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if available_models:
                resp = await client.post(f"{ollama_url}/api/generate",
                                         json={"model": resolved, "prompt": ""})
            else:
                resp = await client.post(f"{ollama_url}/v1/chat/completions", json={
                    "model": resolved,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                    "temperature": 0.0,
                })
            resp.raise_for_status()
    except Exception as e:
        log.warning(f"Warm-up failed  model={resolved}  err={type(e).__name__}: {e}")
        return False
    took = time.time() - started
    if took > 1.0:
        log.info(f"Model loaded  model={resolved}  {took:.1f}s")
    return True


def warm_target(models: list[str], available_models: list[str]) -> str | None:
    """The one model a node keeps loaded: the first chat model it advertises.
    Keeping several warm on a GPU that holds one would make them evict each
    other."""
    for m in models:
        if m == "any":
            chat = [a for a in available_models if "embed" not in a.lower()]
            return chat[0] if chat else None
        if "embed" not in m.lower():
            return m
    return None


# ── Inference engines ─────────────────────────────────────────────────────────

async def run_mock_inference(model: str, messages: list, max_tokens: int, temperature: float) -> tuple[str, int]:
    """Simulated inference for testing without a real model."""
    processing_time = random.uniform(0.5, 2.5)
    await asyncio.sleep(processing_time)

    last_user_msg = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")

    # Seed by message content so two mock nodes always pick the same template,
    # letting redundant verification pass in test scenarios.
    # Use hashlib — Python's hash() is randomized per-process and would cause
    # two nodes to disagree even on identical input.
    stable_seed = int(hashlib.md5(last_user_msg.encode()).hexdigest(), 16)
    rng = random.Random(stable_seed)
    responses = [
        f"[{model} @ DAI node] I've processed your request: \"{last_user_msg[:60]}{'...' if len(last_user_msg) > 60 else ''}\". This is a mock response — run with --ollama for real inference.",
        f"[{model} @ DAI node] Understood. You asked: \"{last_user_msg[:60]}{'...' if len(last_user_msg) > 60 else ''}\". In the full network, a real model would respond here.",
    ]
    content = rng.choice(responses)
    return content, len(content.split()) + len(last_user_msg.split())


async def run_ollama_inference(
    model: str,
    messages: list,
    max_tokens: int,
    temperature: float,
    ollama_url: str,
    available_models: list[str],
    seed: int | None = None,
) -> tuple[str, int]:
    """Real inference via Ollama's OpenAI-compatible API."""
    resolved = await ollama_resolve_model(model, available_models)
    log.info(f"Ollama running  model={resolved}  seed={seed}")

    request_body = {
        "model": resolved,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
        # Disable thinking mode so reasoning tokens don't consume the entire
        # context window, leaving nothing for the actual answer.
        "think": False,
    }
    if seed is not None:
        request_body["seed"] = seed

    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(
            f"{ollama_url}/v1/chat/completions",
            json=request_body,
        )
        resp.raise_for_status()
        data = resp.json()

    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""

    # qwen3 and other thinking models wrap internal reasoning in <think> tags.
    # Strip them — we only want the visible answer.
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

    # If thinking consumed everything and no visible answer remains, fall back
    # to the reasoning_content field that some Ollama versions expose.
    if not content:
        content = (msg.get("reasoning_content") or "").strip()

    tokens_used = data.get("usage", {}).get("completion_tokens", len(content.split()))
    return content, tokens_used


# ── Main node loop ────────────────────────────────────────────────────────────

async def run_node(orchestrator_url: str, node_info: NodeInfo, use_ollama: bool, ollama_url: str,
                   available_models: list[str], warm: str | None = None):
    log.info(f"Connecting to  {orchestrator_url}")
    log.info(f"Node ID:       {node_info.node_id}")
    log.info(f"Models:        {node_info.models}")
    log.info(f"GPU:           {node_info.gpu}")
    log.info(f"Inference:     {'Ollama (real)' if use_ollama else 'Mock'}")

    if warm:
        # Before registering: the orchestrator only sees this node once it can
        # answer in normal time.
        log.info(f"Warming up     {warm} (loading into memory before taking tasks)...")
        await warm_model(warm, ollama_url, available_models)
    last_activity = [time.time()]

    backoff = 1
    while True:
        try:
            async with websockets.connect(orchestrator_url, ping_interval=None) as ws:
                backoff = 1

                register_msg = WSMessage(type="register", payload=node_info.model_dump())
                await ws.send(register_msg.model_dump_json())

                raw = await ws.recv()
                ack = json.loads(raw)
                if ack.get("type") == "ack":
                    log.info(f"Registered. {ack['payload'].get('message', '')}")
                else:
                    log.error(f"Unexpected registration response: {ack}")
                    return

                async def heartbeat():
                    while True:
                        await asyncio.sleep(HEARTBEAT_INTERVAL)
                        try:
                            hb = WSMessage(type="heartbeat", payload={"node_id": node_info.node_id})
                            await ws.send(hb.model_dump_json())
                        except Exception:
                            break

                async def keep_warm():
                    # Ollama only; other backends keep their model loaded.
                    while True:
                        await asyncio.sleep(HEARTBEAT_INTERVAL)
                        if time.time() - last_activity[0] >= KEEP_WARM_INTERVAL:
                            last_activity[0] = time.time()
                            await warm_model(warm, ollama_url, available_models)

                heartbeat_task = asyncio.create_task(heartbeat())
                warm_task = asyncio.create_task(keep_warm()) if warm and available_models else None
                log.info("Waiting for tasks...")

                try:
                    async for raw in ws:
                        msg = json.loads(raw)

                        if msg.get("type") == "task":
                            payload = msg["payload"]
                            task_id = payload["task_id"]
                            model = payload["model"]
                            messages = payload["messages"]
                            max_tokens = payload.get("max_tokens", 512)
                            temperature = payload.get("temperature", 0.7)
                            seed = payload.get("seed")  # None unless model has a registered stack

                            log.info(f"Task received  id={task_id}  model={model}  messages={len(messages)}  seed={seed}")
                            last_activity[0] = time.time()

                            try:
                                if use_ollama:
                                    content, tokens_used = await run_ollama_inference(
                                        model, messages, max_tokens, temperature, ollama_url, available_models, seed=seed
                                    )
                                else:
                                    content, tokens_used = await run_mock_inference(
                                        model, messages, max_tokens, temperature
                                    )

                                result_msg = WSMessage(
                                    type="task_complete",
                                    payload={
                                        "task_id": task_id,
                                        "node_id": node_info.node_id,
                                        "content": content,
                                        "tokens_used": tokens_used,
                                    }
                                )
                                await ws.send(result_msg.model_dump_json())
                                log.info(f"Task complete  id={task_id}  tokens={tokens_used}")

                            except Exception as e:
                                log.error(f"Inference error  id={task_id}  err={e}")
                                fail_msg = WSMessage(
                                    type="task_failed",
                                    payload={"task_id": task_id, "node_id": node_info.node_id, "error": str(e)}
                                )
                                await ws.send(fail_msg.model_dump_json())
                finally:
                    heartbeat_task.cancel()
                    if warm_task:
                        warm_task.cancel()

        except (websockets.exceptions.ConnectionClosedError,
                websockets.exceptions.ConnectionClosedOK,
                ConnectionRefusedError,
                OSError) as e:
            log.warning(f"Connection lost: {e}. Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

        except Exception as e:
            log.exception(f"Unexpected error: {e}. Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ── CLI entry point ───────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="DAI Node Client — join the network and earn by sharing compute.")
    parser.add_argument("--orchestrator", default="ws://localhost:8000/ws/node")
    parser.add_argument("--models", default=None, help="Comma-separated model names (default: auto-detect from Ollama, or 'any' in mock mode)")
    parser.add_argument("--gpu", action="store_true")
    parser.add_argument("--vram", type=float, default=None, help="VRAM in GB")
    parser.add_argument("--ram", type=float, default=None, help="RAM in GB")
    parser.add_argument("--id", default=None, help="Custom node ID")
    parser.add_argument("--ollama", action="store_true", help="Enable real inference via Ollama")
    parser.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama API URL")
    parser.add_argument("--auto", action="store_true", help="Auto-detect installed Ollama models and advertise them")
    parser.add_argument("--wallet", default=os.getenv("DAI_WALLET"), help="EVM wallet address for on-chain rewards (optional in mock mode)")
    parser.add_argument("--project", default=os.getenv("DAI_PROJECT"), help="Dedicate this node to a specific project (only receives tasks tagged with this project)")
    parser.add_argument("--no-warmup", action="store_true",
                        help="Don't load the model before joining or keep it loaded while idle (Ollama unloads idle models after 5 minutes)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    ollama_url = args.ollama_url
    available_models = []

    async def setup():
        global available_models

        if args.ollama or args.auto:
            available_models = await ollama_list_models(ollama_url)
            if not available_models:
                if args.models:
                    # Any OpenAI-compatible backend (llama.cpp llama-server, vLLM,
                    # LM Studio) serves /v1/chat/completions but not Ollama's
                    # /api/tags. If the operator named the models, trust that and
                    # skip auto-detection instead of refusing to start.
                    log.warning("Backend model list unavailable (/api/tags); "
                                "using --models as given.")
                else:
                    log.error("No models found. Is the backend running? "
                              "For Ollama: ollama serve. Otherwise pass --models.")
                    sys.exit(1)
            else:
                log.info(f"Backend models found: {available_models}")

        if args.models:
            models = [m.strip() for m in args.models.split(",") if m.strip()]
        elif available_models:
            models = available_models
        else:
            models = ["any"]

        node_id = args.id or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"

        node_info = NodeInfo(
            node_id=node_id,
            models=models,
            gpu=args.gpu,
            vram_gb=args.vram,
            ram_gb=args.ram,
            wallet=args.wallet or None,
            project=args.project or None,
        )

        use_ollama = args.ollama or args.auto
        warm = None if (args.no_warmup or not use_ollama) else warm_target(models, available_models)

        await run_node(
            orchestrator_url=args.orchestrator,
            node_info=node_info,
            use_ollama=use_ollama,
            ollama_url=ollama_url,
            available_models=available_models,
            warm=warm,
        )

    try:
        asyncio.run(setup())
    except KeyboardInterrupt:
        log.info("Node shutting down.")
