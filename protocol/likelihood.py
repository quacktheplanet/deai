"""
DAI Likelihood check — "did this text come from the claimed model?"
--------------------------------------------------------------------
The embedding comparator answers "is this an answer to this question?" and
cannot tell a cheaper model's answer from the real one's (CROSS_HARDWARE_RESULTS:
a 0.5B model posing as a 7B was caught in only 6 of 30 checks). This check asks
the model itself: a checker holding the registered model forces it to produce
the primary's exact text and reads how likely the model found each token.

  - top1_rate: the share of tokens that were the model's own first choice. An
    honest node's temperature-0 answer is almost all first choices, even on other
    hardware (small numeric differences flip a few near-ties). A substitute's
    text is full of tokens the real model wouldn't have picked.
  - mean_logprob: the average log-probability of those tokens.

It needs no second generation of the task, only the scoring pass, and nothing
semantic: short answers ("Paris") carry too little signal and are reported as
inconclusive rather than passed or failed.

Works with llama.cpp's llama-server (and anything else exposing its
/apply-template and /completion with GBNF grammars and n_probs). The exact
text is forced with a grammar that admits only that string; with
post_sampling_probs false the reported probabilities are the model's own,
before the grammar narrows the choice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass
class LikelihoodScore:
    tokens: int
    top1_rate: float
    mean_logprob: float


@dataclass
class LikelihoodPolicy:
    # From docs/CROSS_HARDWARE_RESULTS.md ("Likelihood check"): 30 honest
    # Qwen2.5-7B answers scored by another llama.cpp build were all >= 0.896
    # first choices and >= -0.569 mean; these catch 23 of 25 judgeable 0.5B
    # substitutes. Margins are thin; widen with more hardware before slashing.
    min_tokens: int = 16        # below this: inconclusive
    min_top1: float = 0.88
    min_mean_logprob: float = -0.65

    def judge(self, s: Optional[LikelihoodScore]) -> Optional[bool]:
        """True = consistent with the model, False = unlikely to be its output,
        None = can't tell (too short, or no score)."""
        if s is None or s.tokens < self.min_tokens:
            return None
        return s.top1_rate >= self.min_top1 and s.mean_logprob >= self.min_mean_logprob


def gbnf_literal(text: str) -> str:
    """A grammar that admits exactly `text`."""
    esc = (text.replace("\\", "\\\\").replace('"', '\\"')
           .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return f'root ::= "{esc}"'


def summarize(probs: list) -> Optional[LikelihoodScore]:
    """From llama-server's completion_probabilities (n_probs >= 1)."""
    lps = [p["logprob"] for p in probs if "logprob" in p]
    if not lps:
        return None
    firsts = [p["top_logprobs"][0]["token"] == p["token"] for p in probs if p.get("top_logprobs")]
    return LikelihoodScore(tokens=len(lps), top1_rate=sum(firsts) / len(firsts) if firsts else 0.0,
                           mean_logprob=sum(lps) / len(lps))


async def score_text(base_url: str, messages: list, text: str, timeout: float = 600.0) -> Optional[LikelihoodScore]:
    """Score `text` as the model's reply to `messages`. None if the backend
    can't (no /apply-template, or the forced text didn't come back intact)."""
    base = base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(base + "/apply-template", json={"messages": messages})
        r.raise_for_status()
        prompt = r.json()["prompt"]
        r = await client.post(base + "/completion", json={
            "prompt": prompt, "grammar": gbnf_literal(text), "n_predict": 8192, "temperature": 0.0,
            "n_probs": 1, "post_sampling_probs": False, "cache_prompt": False})
        r.raise_for_status()
        data = r.json()
    if data.get("content") != text:
        return None
    return summarize(data.get("completion_probabilities") or [])


async def supports_scoring(base_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(base_url.rstrip("/") + "/apply-template",
                                  json={"messages": [{"role": "user", "content": "hi"}]})
            return r.status_code == 200 and "prompt" in r.json()
    except Exception:
        return False
