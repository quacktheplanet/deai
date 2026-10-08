"""
DAI Verification
-----------------
The keystone seam. Replaces the old `mock_verify` non-empty check with a
pluggable `Verifier` interface so the network can evolve from "accept any
non-empty string" toward real Proof-of-Useful-Inference without touching the
orchestrator's dispatch logic again.

This module implements the Standard tier from docs/VERIFICATION.md:

    optimistic redundant execution + economic slashing

Only the *seam* and the *Standard-tier base case* are built here. Per
docs/VERIFICATION.md, the following are deliberately NOT solved yet and are
left as explicit hooks rather than faked:

  - the empirical comparison method (semantic embedding vs. judge model vs.
    logprob agreement) — `default_comparator` is a labelled placeholder;
  - committee escalation and blame attribution when two samples disagree —
    a 2-sample mismatch sets `escalation_required` and does NOT auto-slash,
    because slashing an honest provider on a false positive is flagged
    existential in the design doc;
  - the per-model reference inference stack that makes "two nodes disagree"
    a well-defined statement.

The Attested (TEE) and Proven (zkML) tiers are intentionally absent — they
are future tiers, not stubs.
"""

from __future__ import annotations

import difflib
import logging
import random
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional

from shared.schemas import Task, TaskResult

log = logging.getLogger("verification")


# ── Comparison ────────────────────────────────────────────────────────────────

_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WS.sub(" ", text.strip().lower())


def default_comparator(a: str, b: str) -> float:
    """
    Deterministic, dependency-free similarity in [0, 1].

    PLACEHOLDER. docs/VERIFICATION.md lists the real comparison method as an
    open, empirically-decided question (semantic embedding cosine vs. judge
    model vs. logprob agreement). This whitespace/case-normalized sequence
    ratio is good enough to (a) prove the seam end-to-end and (b) catch a node
    that returns garbage or swapped in a different, much smaller model. It is
    NOT the final comparator and must not be relied on for mainnet economics.
    """
    na, nb = _normalize(a), _normalize(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


class ComparatorUnavailable(Exception):
    """The comparator could not produce a score (e.g. the embedding endpoint is
    down or still loading). Not evidence about either result."""


@dataclass
class VerificationOutcome:
    accepted: bool
    method: str
    rechecked: bool = False
    agreement: Optional[float] = None
    escalation_required: bool = False
    detail: str = ""
    # Accepted without a real comparison (the comparator was unavailable).
    # Paid optimistically like a task with no checker; never escalated.
    unverified: bool = False


# ── Verifier interface ────────────────────────────────────────────────────────

class Verifier(ABC):
    """
    Decision policy for a task result. The orchestrator owns the mechanics
    (node registry, WS dispatch, re-dispatch); the Verifier owns the policy:
    is this result well-formed, should it be rechecked, and do two samples
    agree.
    """

    @abstractmethod
    def well_formed(self, result: TaskResult) -> bool:
        """Cheap local gate. A malformed result never reaches recheck."""

    @abstractmethod
    def should_recheck(self, task: Task, result: TaskResult) -> bool:
        """Sampling decision — the optimistic protocol's probability `p`."""

    @abstractmethod
    def compare(
        self, task: Task, primary: TaskResult, redundant: TaskResult
    ) -> VerificationOutcome:
        """Tolerance comparison of a primary result against a redundant one."""


class ContentVerifier(Verifier):
    """
    The pre-existing behaviour, preserved exactly: accept any non-empty result,
    never recheck. This is the default so mock mode and CI are unchanged until
    an operator explicitly opts into redundant verification.
    """

    def well_formed(self, result: TaskResult) -> bool:
        return bool(result.content and result.content.strip())

    def should_recheck(self, task: Task, result: TaskResult) -> bool:
        return False

    def compare(
        self, task: Task, primary: TaskResult, redundant: TaskResult
    ) -> VerificationOutcome:
        # Unreachable while should_recheck is False; defined for completeness.
        return VerificationOutcome(accepted=True, method="content_only")


class RedundantExecutionVerifier(Verifier):
    """
    Standard tier (docs/VERIFICATION.md, "Optimistic protocol sketch").

    With probability `sample_rate` the orchestrator silently re-runs the task
    on a different node and asks this verifier to compare. Security is
    economic, not cryptographic: the node does not know which tasks are
    checked, so cheating is deterred when stake_at_risk * p > gain_per_cheat.
    """

    def __init__(
        self,
        sample_rate: float,
        agreement_threshold: float = 0.75,
        comparator: Optional[Callable[[str, str], float]] = None,
        rng: Optional[random.Random] = None,
    ):
        if not 0.0 <= sample_rate <= 1.0:
            raise ValueError("sample_rate must be in [0, 1]")
        if not 0.0 <= agreement_threshold <= 1.0:
            raise ValueError("agreement_threshold must be in [0, 1]")
        self.sample_rate = sample_rate
        self.agreement_threshold = agreement_threshold
        self._comparator = comparator or default_comparator
        self._rng = rng or random.Random()

    def well_formed(self, result: TaskResult) -> bool:
        return bool(result.content and result.content.strip())

    def should_recheck(self, task: Task, result: TaskResult) -> bool:
        if self.sample_rate <= 0.0:
            return False
        if self.sample_rate >= 1.0:
            return True
        return self._rng.random() < self.sample_rate

    def compare(
        self, task: Task, primary: TaskResult, redundant: TaskResult
    ) -> VerificationOutcome:
        try:
            score = self._comparator(primary.content, redundant.content)
            how = ""
        except ComparatorUnavailable as e:
            # The sequence ratio can confirm near-identical text, but a low
            # ratio says nothing about paraphrases (two honest answers worded
            # differently score ~0.3). So it may only ever accept: below the
            # threshold the task is unverified, never a mismatch — a mismatch
            # convenes a committee that can slash an honest node.
            score = default_comparator(primary.content, redundant.content)
            if score < self.agreement_threshold:
                return VerificationOutcome(
                    accepted=True,
                    method="unverified",
                    rechecked=True,
                    unverified=True,
                    detail=f"comparator unavailable ({e}); accepted unverified",
                )
            how = " (sequence ratio; comparator unavailable)"
        if score >= self.agreement_threshold:
            return VerificationOutcome(
                accepted=True,
                method="redundant_match",
                rechecked=True,
                agreement=score,
                detail=f"agreement {score:.3f} >= {self.agreement_threshold:.3f}{how}",
            )
        # Two samples disagree. We cannot attribute blame with only two
        # samples, and slashing an honest provider on a false positive is
        # flagged existential in docs/VERIFICATION.md. So: reject this result
        # and require committee escalation. Auto-slashing is intentionally NOT
        # done here.
        return VerificationOutcome(
            accepted=False,
            method="redundant_mismatch",
            rechecked=True,
            agreement=score,
            escalation_required=True,
            detail=(
                f"agreement {score:.3f} < {self.agreement_threshold:.3f}; "
                "committee escalation follows"
            ),
        )


# ── Embedding comparator (semantic cosine similarity) ─────────────────────────

def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class EmbeddingComparator:
    """
    Semantic similarity via embedding cosine distance.

    Decided (docs/VERIFICATION_PROTOCOL.md §4): semantic embedding is the
    chosen direction for the default_comparator replacement. SequenceMatcher
    fails on paraphrase equivalence — two honest nodes producing the same
    answer in different words score low even though both are correct. Cosine
    similarity of embedding vectors handles this correctly.

    Accepts any OpenAI-compatible embedding endpoint:
      - Ollama (v0.1.27+): http://localhost:11434
      - OpenAI API:         https://api.openai.com
      - Any local serving

    Synchronous; the orchestrator calls it from a worker thread so a slow
    call never stalls the event loop.

    A cold embedding model (first call, or Ollama evicted it to make room for
    a chat model) can take far longer than a warm call, so until a call has
    succeeded — and once more after any timeout — the generous
    ``cold_timeout`` applies. ``warm_up()`` loads the model at startup.

    If no score can be had, raises ComparatorUnavailable. It does NOT fall
    back to the sequence ratio itself: that ratio scores honest paraphrases
    ~0.3, which used to turn an embedding outage into false mismatches.
    The verifier decides what an unavailable comparator means.

    Enable with --embedding-url <base_url>; default model nomic-embed-text
    is available via `ollama pull nomic-embed-text`.
    """

    def __init__(self, base_url: str, model: str = "nomic-embed-text",
                 timeout: float = 10.0, cold_timeout: float = 120.0):
        self._url = base_url.rstrip("/") + "/v1/embeddings"
        self._model = model
        self.timeout = timeout
        self.cold_timeout = cold_timeout
        self._warm = False

    def _embed(self, texts: list, timeout: float) -> list:
        import httpx
        resp = httpx.post(
            self._url,
            json={"model": self._model, "input": texts},
            timeout=timeout,
        )
        resp.raise_for_status()
        items = sorted(resp.json()["data"], key=lambda x: x["index"])
        if len(items) != len(texts):
            raise ValueError(f"expected {len(texts)} embeddings, got {len(items)}")
        self._warm = True
        return [x["embedding"] for x in items]

    def warm_up(self) -> bool:
        """Load the embedding model now so the first verification is not the
        one that waits for it. Returns False (and logs) if it could not."""
        try:
            self._embed(["warm-up"], self.cold_timeout)
            log.info(f"Embedding comparator warm  model={self._model}")
            return True
        except Exception as e:
            log.warning(f"Embedding comparator warm-up failed ({e}); will retry on first use")
            return False

    def __call__(self, a: str, b: str) -> float:
        import httpx
        try:
            try:
                ea, eb = self._embed([a, b], self.timeout if self._warm else self.cold_timeout)
            except httpx.TimeoutException:
                if not self._warm:
                    raise
                # Probably evicted and reloading: one more try, cold-sized.
                self._warm = False
                log.warning("Embedding call timed out; retrying with the cold-start timeout")
                ea, eb = self._embed([a, b], self.cold_timeout)
            return _cosine(ea, eb)
        except Exception as e:
            log.warning(f"Embedding comparator unavailable ({type(e).__name__}: {e})")
            raise ComparatorUnavailable(f"{type(e).__name__}: {e}") from e


def make_verifier(
    sample_rate: float,
    agreement_threshold: float = 0.75,
    embedding_url: Optional[str] = None,
    embedding_model: str = "nomic-embed-text",
) -> Verifier:
    """
    sample_rate <= 0  → ContentVerifier (unchanged legacy behaviour, default)
    sample_rate >  0  → RedundantExecutionVerifier (optimistic Standard tier)

    embedding_url: when set, use EmbeddingComparator (semantic cosine) instead
    of the SequenceMatcher placeholder. Any OpenAI-compatible embedding
    endpoint works (Ollama, OpenAI API, local serving).
    """
    if sample_rate <= 0.0:
        return ContentVerifier()
    comparator: Optional[Callable[[str, str], float]] = None
    if embedding_url:
        comparator = EmbeddingComparator(embedding_url, embedding_model)
        log.info(f"Comparator: semantic embedding  url={embedding_url}  model={embedding_model}")
    return RedundantExecutionVerifier(
        sample_rate=sample_rate,
        agreement_threshold=agreement_threshold,
        comparator=comparator,
    )
