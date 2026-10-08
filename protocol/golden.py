"""
DAI Golden Set — known-answer tasks (VERIFICATION_PROTOCOL.md §9)
------------------------------------------------------------------
Prompts whose reference output was computed once, offline, on the model's
registered reference stack (§12). They make two cheap checks possible, each
needing ONE node instead of two:

  - qualification: a node joining with model M answers a few of them before
    it gets paid work for M. Passing shows it really runs M now; the time it
    took gives a measured speed instead of a self-reported GPU claim;
  - canaries: the orchestrator slips them into the normal flow of work. The
    node can't tell them apart from real requests, so they deter cheating as
    well as sampled re-execution does, without running anything twice.

Comparison uses the orchestrator's comparator and threshold, so a canary is
judged exactly like a redundant check against an honest node's answer.

Build a set (operator, offline, against a reference-stack backend):

    python protocol/golden.py build --model qwen2.5:14b --url http://localhost:11434 \\
        --seed 42 --out golden.json [--prompts prompts.json]

then start the orchestrator with --golden-file golden.json.

Open problem carried from §9: a node that learns to recognise golden prompts
answers them honestly and cheats elsewhere. The set needs to be large, fresh
and look like real traffic; the built-in prompt bank is for testing only.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class GoldenEntry:
    model_id: str
    messages: list
    reference: str
    max_tokens: int = 512
    temperature: float = 0.0
    seed: Optional[int] = None
    built_at: str = ""
    built_with: str = ""   # backend / runner the reference came from, for audit


@dataclass
class GoldenSet:
    entries: list = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "GoldenSet":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([GoldenEntry(**e) for e in data.get("entries", [])])

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps({"entries": [asdict(e) for e in self.entries]}, indent=2) + "\n",
                              encoding="utf-8")

    def models(self) -> set:
        return {e.model_id for e in self.entries}

    def has(self, model_id: str) -> bool:
        return any(e.model_id == model_id for e in self.entries)

    def for_model(self, model_id: str) -> list:
        return [e for e in self.entries if e.model_id == model_id]

    def sample(self, model_id: str, k: int, rng: Optional[random.Random] = None) -> list:
        pool = self.for_model(model_id)
        rng = rng or random
        return rng.sample(pool, min(k, len(pool)))


# A small default bank for trying the mechanism out. Real deployments need a
# large, refreshed set that looks like real traffic (see module docstring).
DEFAULT_PROMPTS = [
    "What is the capital of Australia? Answer in one word.",
    "Explain what a hash table is in two sentences.",
    "List three differences between TCP and UDP.",
    "Write a Python function that returns the factorial of n.",
    "Summarise the water cycle in under 60 words.",
    "What does HTTP status code 404 mean?",
    "Translate 'good morning, how are you?' into Spanish.",
    "Give two reasons unit tests are useful.",
    "What is the derivative of x squared?",
    "Describe a binary search in one paragraph.",
    "Name the planets of the solar system in order from the Sun.",
    "What is the difference between a process and a thread? Keep it under 80 words.",
]


def build(model: str, url: str, prompts: list[str], seed: int, max_tokens: int, out: str) -> int:
    import httpx
    gs = GoldenSet.load(out) if Path(out).exists() else GoldenSet()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for p in prompts:
        messages = [{"role": "user", "content": p}]
        r = httpx.post(url.rstrip("/") + "/v1/chat/completions", timeout=600, json={
            "model": model, "messages": messages, "temperature": 0.0, "seed": seed,
            "max_tokens": max_tokens, "stream": False})
        r.raise_for_status()
        text = (r.json()["choices"][0]["message"].get("content") or "").strip()
        if not text:
            print(f"  skipped (empty answer): {p[:50]}")
            continue
        gs.entries.append(GoldenEntry(model_id=model, messages=messages, reference=text, max_tokens=max_tokens,
                                      seed=seed, built_at=stamp, built_with=url))
        print(f"  ok  {p[:60]}")
    gs.save(out)
    print(f"{len(gs.for_model(model))} entries for {model} in {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a golden (known-answer) set for a model.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--model", required=True, help="model id exactly as nodes advertise it")
    b.add_argument("--url", default="http://localhost:11434", help="OpenAI-compatible backend running the reference stack")
    b.add_argument("--seed", type=int, required=True, help="the registered stack's seed")
    b.add_argument("--max-tokens", type=int, default=512)
    b.add_argument("--prompts", help="JSON list of prompt strings (default: a small built-in bank)")
    b.add_argument("--out", required=True)
    args = ap.parse_args()
    prompts = json.loads(Path(args.prompts).read_text()) if args.prompts else DEFAULT_PROMPTS
    return build(args.model, args.url, prompts, args.seed, args.max_tokens, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
