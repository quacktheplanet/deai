# DAI — Optimistic Verification Protocol (Spec)

> Status: spec doc. This is build-now item #1 from
> [VERIFICATION.md](VERIFICATION.md) — the full specification of the **Standard
> tier** (optimistic redundant execution + economic slashing) for VISION.md
> **Stage 0–1 only**. It is the design the `Verifier` seam shipped in
> `protocol/verification.py` must converge to. Consistent with VERIFICATION.md
> (the decision) and ECONOMICS.md §4–§6 (sybil bond, slash magnitude, the
> shared work-measurement). Empirical parameters are explicitly deferred to the
> non-transferable testnet — this doc specs the *framework and invariants*, not
> the numbers. §8 is a live conformance ledger so code and spec cannot drift.

---

## 0. Scope

**Specified here:** the Standard verification tier for Stage 0 (one node / one
task) and Stage 1 (job-parallel sub-tasks): roles, the task lifecycle state
machine, the sampling rule, the comparison framework, the escalation/slash
flow, the threat model, and a parameter register.

**Not specified here (deferred, by design):**
- Attested (TEE) and Proven (zkML) tiers — future tiers, not stubs
  (VERIFICATION.md).
- Stage 2 model-sharded reward attribution — explicitly deferred, do not
  over-design (ECONOMICS.md §3).
- The numeric value of every empirical parameter — testnet-tuned
  (ECONOMICS.md §5, "Deferred / open").

**One-sentence why:** a node claims "I ran model M on prompt P and got O"; the
network must make that claim economically unprofitable to fake, because the
redemption rate (ECONOMICS.md §1) is computed from this *same* verified
work-measurement — rate honesty is downstream of verification honesty, one
problem not two.

---

## 1. Roles & definitions

- **Requester** — submits an inference task via the HTTP API.
- **Orchestrator** — owns the node registry, dispatch, sampling decision,
  re-dispatch, and (today) settlement. Currently a single trusted node
  (disclosed; decentralizing it is a separate roadmap item).
- **Primary** — the node that produced the result returned to the requester.
- **Checker** — a *different* node the same task is silently re-dispatched to.
- **Committee** — N independent nodes consulted when primary and checker
  disagree. (Not yet implemented — see §5, §8.)
- **Reference inference stack (per model)** — the pinned definition of "the
  same model": runtime + version, quantization, decode params (temperature 0,
  fixed seed), max tokens. Without this, "two nodes disagree" is not a
  well-defined statement (VERIFICATION.md, "Why LLM verification is hard").
  **A model is not eligible for the Standard tier until its reference stack is
  registered.** No model registry exists yet — this is the single largest
  unbuilt prerequisite.

A result is **honest** iff it is what the model's registered reference stack
produces for P, within the tier's comparison tolerance. Everything below
defines how the network decides that economically rather than cryptographically.

---

## 2. Task lifecycle (state machine)

```
SUBMITTED
  └─> DISPATCHED (primary)            orchestrator: find_best_node, send
        └─> RESULT_IN                  primary returns task_complete
              └─> WELL_FORMED?         cheap local gate (non-empty etc.)
                    ├─ no  ─> REJECTED_MALFORMED   (slash path, §5.1)
                    └─ yes ─> SAMPLED?  with probability p (§3)
                              ├─ no  ─> FINALIZED            (optimistic accept)
                              └─ yes ─> RECHECK_DISPATCHED (checker ≠ primary)
                                          └─> COMPARED (§4)
                                                ├─ agree    ─> FINALIZED
                                                ├─ no checker free ─> FINALIZED*
                                                ├─ comparator down ─> FINALIZED*
                                                └─ disagree ─> DISPUTED
                                                                  └─> ESCALATED (§5)
                                                                        ├─ primary upheld ─> FINALIZED
                                                                        └─ primary dishonest ─> SLASHED
```

- `FINALIZED` = result returned, reward **accrues off-chain** to the primary.
  Build-now #4 is **done**: there is no per-task mint; cumulative accrual is
  settled once per epoch as a Merkle root via `MerkleDistributor`, and miners
  claim themselves. Under the target economics this accrual is still meant to
  be **unvested/bonded** until a vesting period elapses (ECONOMICS.md §4) —
  that vesting layer, and slash-reduces-accrual, are the documented next step
  (the accrual seam is built to accept them without rework).
- `FINALIZED*` (no checker available) = accept optimistically rather than
  punish a provider for a thin network; the task is recorded as *unverified*.
  This is a deliberate availability-over-strictness choice for the bootstrap
  phase and must be revisited before mainnet (an attacker could DoS the checker
  pool to force this branch). The same applies when the comparator itself is
  unavailable (e.g. the embedding model is still loading): its failure says
  nothing about either result, so it must never become `DISPUTED`. The
  sequence ratio may still *confirm* near-identical text, but a low ratio is
  not evidence (honest paraphrases score ~0.3). A comparator failure during a
  committee tally likewise falls back to `FINALIZED*`. The orchestrator
  reports which outcome a task got in the `X-DAI-Verification` response header
  (`verified` / `unverified` / `unchecked`) and in `/status` stats.
- `DISPUTED → ESCALATED` is currently a **stub**: the implementation rejects
  the result to the requester and flags `escalation_required`; it does **not**
  convene a committee and does **not** auto-slash. This is intentional (§5.2).

---

## 3. Sampling probability `p`

**Invariant (decided).** Cheating must be -EV:

```
expected_penalty(slashable_bond, p)  >  expected_gain_per_cheat
```

(ECONOMICS.md §5.) The attacker must not be able to tell which tasks are
checked — sampling is **silent** (the checker dispatch is an ordinary task; the
node is never told it is a recheck). Security is economic, not cryptographic.

**Decided:**
- `p` is a protocol parameter, applied per task, silent.
- Recheck always lands on a node *other than* the primary (`exclude` set).
- `p` may be **conditioned** (higher for: new/low-reputation nodes, high-value
  tasks, models whose recent dispute rate is elevated). The conditioning
  inputs must themselves be tamper-evident (they feed economics).

**Deferred (empirical, testnet):** the baseline numeric `p`, the conditioning
schedule, and how `p` trades off against `slash_fraction` and the comparison
threshold (jointly optimised — ECONOMICS.md §5).

**Today:** `p` is a single static knob (`--verify-sample-rate`, default `0.0`).
Default-off is the *bootstrap* setting (legacy behaviour, no behaviour change
on merge), **not** the target — the target is `p > 0`, conditioned, once
reference stacks and committee escalation exist.

---

## 4. Comparison

The hard part. Re-running M(P) on a second node does **not** yield byte-equal
output across heterogeneous consumer hardware even at temperature 0
(VERIFICATION.md). So comparison is **tolerant**, and the tolerance method is a
*pluggable policy*, decided empirically.

**Decision framework (which method, when):**

| Method | Use when | Cost | Status |
|---|---|---|---|
| Exact / normalized-exact | reference stack provably deterministic for that model+hardware class | trivial | only with a strong reference stack |
| Semantic similarity (embedding cosine ≥ T) | general default for free-form output | low | **chosen — see below** |
| Judge-model verdict | high-value or structured tasks | medium (an inference itself) | ruled out as default: recursion risk, latency, cost |
| Logprob agreement | when both nodes can return token logprobs | low | ruled out as default: not universally supported by Ollama |

**Decided:** comparison is tolerant, pluggable behind the `Verifier` seam, and
selected per tier/model — *not* one global hardcoded rule. A model's
registered reference stack determines which methods are even admissible.

**Decided (comparator choice):** semantic embedding cosine similarity is the
chosen direction for the Standard-tier default comparator. Rationale:

- *Logprob agreement* ruled out: logprob support is model- and runtime-dependent
  and is not universally available in Ollama; it also requires protocol changes
  to surface per-token probabilities through the task result schema.
- *Judge model* ruled out as default: adds a full inference round-trip per
  verification check (latency + cost), introduces a recursive trust question
  (who watches the judge?), and creates a single point of failure. May be
  considered for high-value tasks in a future tier.
- *Semantic embedding cosine*: small embedding models (nomic-embed-text,
  all-MiniLM-L6) run locally, have ~50ms latency against Ollama, require no new
  Python dependencies (just an HTTP call the orchestrator already makes), and
  handle paraphrase equivalence — the main failure mode of the SequenceMatcher
  placeholder (two honest nodes saying the same thing in different words).

**Deferred (still empirical):** the threshold `T` and the specific embedding
model. These must be calibrated on testnet data across a representative sample
of prompt types. Starting point: `T = 0.85`, `nomic-embed-text`.

**Today:** `EmbeddingComparator` is implemented and wired to
`--embedding-url`. When `--embedding-url` is not set, `default_comparator`
(whitespace/case-normalized sequence ratio) is still used as the fallback so
CI and no-Ollama deployments are unchanged. Operators opt into semantic
comparison by pointing at their Ollama instance:

```bash
python protocol/orchestrator.py \
  --verify-sample-rate 1.0 \
  --embedding-url http://localhost:11434
# requires: ollama pull nomic-embed-text
```

Replacing it in the future requires no orchestrator change — that is the point
of the `Verifier` seam.

---

## 5. Escalation & slashing

### 5.1 Malformed result (unambiguous)

Empty / non-well-formed output has no attribution ambiguity. Current behaviour
(preserved from before the seam): reject to the requester, and on-chain slash
the producing node when running in chain mode. This stays.

### 5.2 Verified disagreement (the dangerous case)

A primary/checker disagreement does **not** by itself identify the liar — with
two samples either node could be the dishonest one, or the comparison could be
a false positive from inference non-determinism. **Slashing an honest provider
is existential** (providers are the scarce side — VERIFICATION.md,
ECONOMICS.md §5). Therefore:

**Decided:**
1. A two-sample disagreement **never** auto-slashes. It transitions to
   `DISPUTED`, the requester gets an error (not a possibly-wrong answer), and
   no reward is finalized for that task.
2. Slashing requires **committee adjudication**: re-dispatch to N independent
   nodes; the majority output (under the same tolerance) defines ground truth;
   the dissenting minority is judged dishonest.
3. The penalty is taken from the dishonest node's **unvested earned bond**
   (ECONOMICS.md §4) — never from capital it never had to post (no-barrier-to-
   entry is preserved) — its accrued-unfinalized reward is reversed, and
   slashed tokens are **burned** (ECONOMICS.md §2 sinks).
4. False-positive defenses: never slash on a single checker; conservative
   agreement threshold; an **appeal window** before a slash is irreversible;
   elevated `p` on a node's *subsequent* tasks rather than an immediate
   high-severity penalty on a first lone disagreement.

**Deferred (empirical / open):** committee size N and quorum, appeal-window
mechanics, the slash fraction of bond, and collusion-resistant committee
selection (§6). Magnitudes are testnet-tuned (ECONOMICS.md §5).

**Today:** steps 2–4 are **not built**. The implementation stops at step 1
(`DISPUTED`, reject, `escalation_required`, no slash). This is the correct
*safe* partial state — it withholds payment from a possibly-cheating node
without risking an honest-node slash — but it is *incomplete*, not done.

## 6. Threat model

| Attack | Mechanism | Mitigation | Status |
|---|---|---|---|
| Garbage / model-swap | return junk or a cheaper smaller model's output | tolerant comparison vs reference stack catches it | comparator placeholder; reference stack unbuilt |
| Lazy/copying checker | checker returns junk or echoes primary instead of re-executing | independent re-execution; sometimes route to a trusted reference node; committee | committee/reference-node selection unbuilt |
| Collusion / sybil | attacker runs both primary and checker | random checker selection + identity must cost (the vesting bond, ECONOMICS.md §4) + trusted reference nodes | **open** — hard when attacker controls a large share of a small network; tied to the economics/sybil workstream |
| Checker-pool DoS | starve checkers to force the `FINALIZED*` (no-checker) branch | trusted reference fallback; cap unverified throughput | open; bootstrap accepts this risk knowingly |
| Honest-node griefing | adversary tries to trigger false-positive slashes of honest providers | §5.2 defenses (committee, appeal, bond-only, conservative threshold) | escalation unbuilt → currently no slash, so not yet exploitable |

The collusion row is the deepest open problem and is **explicitly shared with
the economics workstream** — verification security and sybil cost are one
problem, as ECONOMICS.md §4–§5 state.

---

## 7. Parameter register

| Parameter | Meaning | Status | Set where |
|---|---|---|---|
| `p` | per-task recheck probability | bootstrap default `0.0`; target `>0` & conditioned | `--verify-sample-rate` / env |
| `agreement_threshold` | accept iff similarity ≥ this | bootstrap default `0.85`; final value empirical | `--verify-threshold` / env |
| comparison method | how two outputs are compared | **decided**: semantic embedding cosine; threshold empirical | `--embedding-url` / `DAI_EMBEDDING_URL`; when unavailable: sequence ratio may confirm a match, otherwise `FINALIZED*` (never a mismatch) |
| reference stack / model | pinned runtime+quant+decode+seed | **deferred** (no registry yet) | future model registry |
| committee size `N`, quorum | adjudication panel | **deferred** | unbuilt |
| appeal window | delay before slash is final | **deferred** | unbuilt |
| `slash_fraction` | portion of unvested bond burned | **deferred** (ECONOMICS.md §5) | unbuilt |
| checker selection policy | how the checker/committee is chosen | **deferred** (collusion-sensitive) | unbuilt |
| canary rate | fraction of tasks that are interleaved known-answer traps (§9) | **research/planned** | unbuilt |
| qualification set | golden challenges gating tier admission (§9) | **research/planned** | unbuilt |
| judge / verification pool | staked large-model adjudicators (§11) | **research/proposed** | unbuilt |

"Bootstrap default" = a safe placeholder that changes no existing behaviour,
not the intended production value.

**Threshold boundary (observed, issue #10).** `T = 0.85` was a reasonable
starting point but a short factual answer was seen to score `0.831` between two
honest nodes — a false-positive boundary case. The threshold may need to be
*length-aware* (a lower floor for short responses), and mismatch lines should
log a prompt hash so boundary cases can be investigated. This is exactly the
empirical tuning §4 defers to testnet, now with a concrete data point.

---

## 8. Conformance ledger (code ⇄ spec — no drift)

What `protocol/{verification,orchestrator,ledger,chain_ledger,merkle,model_registry,committee}.py` +
`chain/contracts/MerkleDistributor.sol` implement **today**:

- ✅ `Verifier` seam; `ContentVerifier` default (legacy non-empty, no recheck);
  `RedundantExecutionVerifier` (well-formed gate, silent sampled re-dispatch to
  a *different* node, tolerant compare).
- ✅ Disagreement → reject + `escalation_required`, **no auto-slash** (§5.2
  step 1).
- ✅ No-checker-free → optimistic accept, logged unverified (§2 `FINALIZED*`).
  Same for an unavailable comparator; the embedding model is warmed at
  orchestrator start and gets a longer timeout while cold.
- ✅ `p` / threshold as static knobs, default-off (§3, §7).
- ✅ **Committee adjudication, appeal window, verified-dishonesty slash**
  (§5.2 steps 2–4, fully specified in §13): `protocol/committee.py` +
  orchestrator integration. `_convene_committee` picks N idle eligible nodes
  excluding primary+checker (uniformly at random per §13.2), dispatches the
  same task in parallel under `_committee_timeout`, tallies votes per §13.4,
  and on a `CHECKER_UPHELD` / `PRIMARY_UPHELD` verdict schedules a delayed
  slash via `_schedule_slash` (appeal window per §13.6). The slash reduces
  the in-memory bond and — in chain mode — the off-chain `_accrued_wei`
  bond and calls `SlashingContract.slash`. ❌ still deferred (§13.9): the
  appeal *mechanism* itself (the window is currently a time-lock only),
  reputation-weighted committee selection (random pool now), the on-chain
  *dispute-slash* bridge as a distinct contract path, and committee
  compensation economics. Parameters (committee_size, timeout, appeal
  window, slash_fraction) are CLI-configurable; defaults are §13.8 starting
  points.
- ✅ **Per-model reference inference stack / model registry** (§12):
  `protocol/model_registry.py` (`ModelStack` + `ModelRegistry` with
  Standard-tier eligibility = temperature 0 + seed set). Persistence
  (§12.6): atomic save / load / auto-save via `--registry-file`. ❌ still
  open: weight-digest enrollment, on-chain registry commit for tamper
  evidence, runtime-version ranges across heterogeneous hardware, and
  stack versioning/migration.
- ✅ Off-chain accrual + claimable batch settlement (build-now #4):
  `MerkleDistributor.sol` + `protocol/merkle.py`; orchestrator accrues and
  publishes a cumulative root per epoch; `/claim/{wallet}` serves proofs;
  no per-task mint; orchestrator no longer holds token MINTER_ROLE. ❌ still
  open: the **vesting/bond** wrapper and slash-reduces-accrual (ECONOMICS.md
  §4) — accrual is built to accept them but credits without a vesting delay
  today. Verification status of the Solidity: it is **compiled** by CI
  (`npx hardhat compile`, real solc) and compiles cleanly; its behavioural
  test (`chain/test/MerkleDistributor.test.js`) runs via
  `npm run test:merkle`, **not yet in the CI gate** — wiring two Hardhat
  test files into one `npm test` run hit a multi-file mocha/network
  lifecycle issue that is a tracked follow-up. The Python side
  (`merkle.py`) is unit-tested in CI and its roots were cross-checked
  against the JS tree.
- ✅ Comparison method decided: `EmbeddingComparator` (semantic embedding cosine
  via OpenAI-compatible `/v1/embeddings` endpoint). Enabled with
  `--embedding-url`; falls back to SequenceMatcher when not configured.
  Threshold `T` is still empirical/deferred.
- ❌ Conditioned/adaptive `p`.
- ✅ Self-reported hardware (GPU/VRAM) removed from pay and routing score
  (build-now #3, the verifiable half). The *measured* benchmark/tier
  replacement (§1, §7) remains ❌ deferred.
- ✅ **Checkers earn nothing today** — this is a known gap, not an oversight.
  The economics of checker compensation (a fraction of primary reward, or a
  separate verification fee) are deferred to the economics workstream
  (ECONOMICS.md §4–§5). The routing fix ensures primary/checker slots
  alternate fairly across nodes so no single node is permanently relegated
  to unpaid checking.

**Smoke-tested (mock + real Ollama, local):** single-node round-trip,
real Ollama inference (qwen3:8b), redundant verification with two mock
nodes (agreement 1.000), redundant verification with two real Ollama nodes
(3/3 factual questions passed at threshold 0.85), three-node load
distribution (9 tasks split 3/3/3). Six routing and dispatch bugs were
found and fixed during this testing — see git log.

**Long-form agreement test (two real Ollama nodes, `EmbeddingComparator`,
qwen3:8b, temperature 0.0, threshold 0.85):** 14/15 prompts passed across
three categories. Scores: factual 1.000 (5/5); explanatory 0.977–0.994
(5/5); creative 0.899–0.958 (4/5). The one failure was a 500 (empty
content after thinking-token budget exhaustion at max_tokens=512), not a
semantic mismatch — every prompt that produced output passed. Lowest
agreement score was 0.899 (robot story, creative category), confirming
0.85 is a safe threshold with adequate margin. Two bugs found and fixed
during this run: `temperature or 0.7` treated `0.0` as falsy (fixed to
`if ... is not None`); `ollama_resolve_model("any", ...)` picked
`nomic-embed-text` when it appeared first in the Ollama model list (fixed
to skip models containing "embed"). **Not yet tested:** node reconnect
after mid-session drop; Sepolia chain mode end-to-end.

Any change to those files must update this section in the same commit.

---

## 9. Reducing redundancy — the network shares work, it does not multiply it

> Principle. Redundant re-execution is a *sampled economic tax*, not the
> operating mode. The product is *sharing* compute — different tasks to
> different nodes, and (VISION Stage 2) one inference split across nodes. The
> only work ever repeated is the sampled check, whose steady-state overhead is
> ≈ `p` (a few percent), not 2×. `--verify-sample-rate 1.0` is a test setting,
> never production. The layers below drive the need for *live* redundancy
> toward the exception.

**Operational vs. adversarial faults — two different "bad node" problems.**
- *Operational* (down, slow, OOM, erroring, empty/malformed, wrong language,
  truncated, degenerate repetition): caught instantly and nearly free by
  liveness/telemetry + the `well_formed` gate. This is the only failure a
  trusted datacenter ever has.
- *Adversarial-but-healthy* (a node that quietly runs a cheaper model and
  returns fluent, plausible output to pocket the difference): invisible to
  monitoring. This is the only thing verification is actually for.

**Pre-flight qualification (planned, cheap).** Before a node gets paid work for
model M, send it *golden challenges* — prompts whose reference output (or
reference embedding under the pinned reference stack, §1) is precomputed once,
offline. Pass → admitted to the M tier, and the measured latency/throughput
*sets its capability tier* — the measured-tier replacement for the deleted
self-reported GPU flag (build-now #3). Cost: one node-side inference per
challenge, no second live node. Proves the node genuinely runs M *now*; does
not prove it stays honest later — hence canaries.

**Canary / known-answer tasks (research → planned, cheap).** Interleave golden
tasks into the real workload, made *indistinguishable* from genuine requests.
Each canary's answer is precomputed, so the check is a cheap comparison with
**no second live node** — yet deterrence equals redundancy sampling, because
the node can't tell which task is a trap.
- *Open challenge:* indistinguishability/freshness. A node that fingerprints
  canaries answers them honestly and cheats the rest. Needs a large, refreshed,
  realistic golden set, ideally drawn from real traffic.
- *Coverage gap:* you can't pre-know the answer to a novel user prompt, so
  canaries *complement* — they don't replace — redundancy/TEE for genuinely
  new work.

**Built (cheap-checks branch, unit-tested; not yet run against real nodes).**
`protocol/golden.py` builds a known-answer set offline against the reference
stack (`python protocol/golden.py build --model M --seed S --out golden.json`).
With `--golden-file`, the orchestrator can:
- qualify (`--qualify-challenges N`): a node joining with a model in the set
  answers N golden tasks before it gets work for that model; a majority must
  match the reference under the normal comparator and threshold. Their timing
  gives `measured_tokens_per_s` in `/status`, the first measured capability
  figure (nothing is scored on it yet);
- send canaries (`--canary-interval S`): about one every S seconds, a golden
  task goes to a random idle, cleared node in exactly the same message format
  as real work, and a match is paid like work, so neither the task nor its
  payment gives it away.

A miss is treated as evidence, not proof (honest hardware differs; the
reference is one sample): it never slashes. A node that missed its last canary
has every paid task rechecked until it passes one, and
`--canary-max-fails` misses in a row (default 3) stop it getting work. A
comparator outage makes a check inconclusive, never a failure. The
indistinguishability problem above is unsolved: the built-in prompt bank is
for testing only.

**The layered picture (the reduction goal).** qualification (screen in, set
tier) → canaries + reputation (cheap ongoing checks; lower `p` for proven
nodes) → redundancy (fallback for novel prompts on untrusted nodes) → TEE
attestation (removes live verification where the hardware exists) → zkML
(eventual everywhere-answer). Redundancy becomes the exception and shrinks over
time. Most of this is buildable in mock mode with the existing
`EmbeddingComparator`; all of it depends on the reference inference stack (§1).

---

## 10. What verification certifies — honest execution, not answer quality

Two questions sound alike and must be kept apart:
1. **Did the node faithfully run the model it was paid for?** (Integrity.)
   Answerable — via redundancy, canaries, or TEE. *This* is the protocol's job
   and what the economics need.
2. **Is the answer genuinely good/helpful?** (Absolute quality.) AI-hard,
   subjective, task-dependent — and *not* the protocol's job. Quality is a
   property of the model the *user* chose; the node's only duty is to run it
   faithfully. Making the protocol an answer-quality judge is the wrong target,
   the same mistake as trying to "guarantee a node's specs."

**Token count is a billing unit, not a quality signal.** Reward is paid per
output token and the count is node-self-reported. Treating "more tokens =
better" would reward verbosity-farming and let nodes inflate their own pay.
Quality is non-monotonic in length anyway (truncation → too few; repetition →
too many). Use token count for *how much work*, never for *how good*.

**The cheap heuristic floor.** Format/schema validity, language detection,
repetition/degeneration and truncation detection catch *low* quality almost
free but cannot *certify high* quality (a floor, not a ceiling). Worth wiring
in as an early reject for broken output — and it directly addresses the
qwen-thinking truncation class (issue #8).

**Specs are the wrong target; verify work.** A software sandbox runs on the
operator's machine — they own the kernel and hypervisor, so it cannot attest
hardware specs to the network (you can't trust a measurement taken on hardware
the adversary controls). Only *hardware* attestation (TEE) can. And you mostly
don't need specs at all: pay for *verified delivered work* and whether a node
"really" had 80 GB is irrelevant (build-now #3). Sandboxing's legitimate role
is isolating the workload and prompt privacy (which TEE also gives) — not
proving capacity.

---

## 11. The verification pool (judge tier) — research / proposed

An opt-in tier of capable, **staked** nodes running *larger* models that act as
judges/adjudicators, rewarded for verification. It is simultaneously a concrete
implementation of the §5.2 committee, an economic role for high-end hardware,
and a premium "judge-verified" assurance lane users can choose and pay for.

**Why it could help the network:**
- Gives expensive GPUs a purpose beyond plain inference → more high-end
  capacity joins → network health and depth.
- A larger model judging a smaller model's output is a credible
  correctness/quality signal (the standard LLM-as-judge asymmetry).
- Operationalizes committee escalation (§5.2): disputes route to the pool's
  staked judges; majority rules.
- Creates a tiered trust market: cheap optimistic tier for routine work; paid
  judge/attested tiers for high-stakes work.

**Honest effects and tradeoffs (must be designed for):**
- *Cost & who pays.* A judge verdict is a whole extra (larger) inference.
  Funded by a premium verification fee the user opts into, or a protocol sink —
  not free.
- *Recursion — who verifies the verifiers?* Judges are nodes too and can be
  lazy or collude. Mitigate with canaries among the pool, staking, reputation,
  random assignment.
- *Centralization pressure.* A pool of expensive nodes is fewer, richer
  operators — tension with the consumer-hardware ethos. Keep membership
  permissionless but stake/qualification-gated; rotate; require many judges.
- *Judge fallibility / bias / prompt-injection.* An LLM judge is imperfect and
  gameable, so a verdict must feed *committee majority*, never unilateral
  slashing — the false-positive-on-an-honest-node risk (§5.2) is existential.
- *Collusion (judge + worker).* Random, anonymous assignment + stake at risk.

**Net ecosystem effect:** a healthy addition *if* cost and centralization are
managed — it monetizes large hardware, strengthens dispute resolution, and lets
users buy assurance proportional to what's at stake. Slots into the tier model
(VERIFICATION.md), §4 (judge-model comparison), §5 (the committee), and
ECONOMICS.md (reward source + premium tier). Not decided — flagged for design.

**How we'll decide if it works — staged validation (cheapest-fatal-first).**
Run the stages in order; a failed gate reworks or drops the idea before money is
spent. The detailed mechanics (committee size/quorum, judge-selection algorithm,
fee/reward split, anti-collusion specifics, orchestrator wiring) are
deliberately left unspecified until Stage A–B produce real numbers — speccing
them earlier would be guesswork.

- **Stage A — Offline judge-accuracy study (no budget; the kill switch).** Build
  a labeled set of prompts × {honest output from the real model; "cheat"
  outputs from cheaper / wrong / subtly-degraded models}. Run candidate judge
  models as classifiers and measure the cheat-catch rate (true positive) and —
  decisively — the honest-node flag rate (false positive), across prompt types
  and lengths (ties to the §7 short-prompt boundary). *Gate:* if no
  configuration keeps honest false-positives near zero while catching cheats,
  the pool fundamentally does not work — stop here. This is largely the same
  accuracy/threshold study already owed for the comparison method.
- **Stage B — Economic simulation (no budget).** Model judge inference cost vs.
  the premium fee a user would tolerate vs. cheat gain vs. sampling rate.
  *Gate:* the fee must cover judge cost *and* the expected penalty must deter
  cheating. If the economics never close, it is a non-starter or needs an
  explicit subsidy.
- **Stage C — Mock-mode mechanism prototype (no budget).** Build dispute →
  route to pool → collect multiple verdicts → majority → outcome, with fake
  judge nodes (as in the redundancy smoke test). Proves buildability and
  exercises the random-assignment anti-collusion routing.
- **Stage D — Testnet adversarial pilot (valueless tokens).** Real mixed-size
  Ollama nodes, a real pool, nodes deliberately cheating; measure real
  false/true-positive rates, latency, cost, and pool concentration over time.

*Commit only if* A shows low honest false-positives, B's economics close, C
proves buildability, and D holds under adversarial load. Fail any stage → fix
or drop, cheaply, because the fatal questions are front-loaded.

---

---

## 12. Reference Inference Stack

> Status: **designed** (this section). Not yet implemented — no registry
> exists. Models currently run under `ContentVerifier` (no redundant check)
> until a stack is registered. This is the single largest prerequisite before
> `RedundantExecutionVerifier` can be used safely on mainnet.

### 12.1 Why it exists

"Two nodes disagree" is not a well-defined statement unless both nodes ran
*exactly the same model configuration*. Even at temperature 0, different
runtime versions, quantization formats, or decode settings produce different
outputs for the same prompt. Without a pinned reference, a comparison
threshold is meaningless — a 0.80 cosine score might mean "both honest nodes,
minor phrasing variation" or "one node ran the wrong model."

The reference stack is the definition of ground truth per model. It is a hard
prerequisite for Standard-tier verification. A model that lacks a registered
stack runs under `ContentVerifier` (accept any non-empty result) — not
`RedundantExecutionVerifier` — because the comparison would be noise.

### 12.2 What a reference stack contains

A registered stack is an immutable record with the following fields:

| Field | Description | Example |
|---|---|---|
| `model_id` | Canonical model identifier | `qwen3:8b` |
| `runtime` | Inference runtime name + minimum version | `ollama>=0.6.0` |
| `format` | Quantization / file format | `Q4_K_M` (GGUF) |
| `temperature` | Decode temperature — **must be 0** | `0` |
| `seed` | Fixed RNG seed — **must be set** | `42` |
| `max_tokens` | Upper bound passed to runtime | `2048` |
| `stop_tokens` | Explicit stop sequences if any | `[]` |
| `system_prompt` | Fixed system-prompt prefix if any | `""` |
| `digest` | SHA-256 of the model weights file(s) | `sha256:...` |
| `registered_at` | Unix timestamp of registration | `1748000000` |
| `registered_by` | Orchestrator wallet that signed registration | `0x...` |

`temperature=0` and a fixed `seed` are **required** — a model that cannot be
run deterministically cannot participate in Standard-tier verification. The
`digest` binds the stack to exact weights, preventing a node from claiming
compliance while running a smaller quantization or a different model entirely.

### 12.3 How a stack is registered

Registration is an orchestrator-signed operation. In the current (Stage 0–1)
architecture:

1. An operator prepares a stack definition JSON matching §12.2.
2. The orchestrator's admin endpoint (`POST /admin/model-registry`) accepts the
   definition, validates required fields, and appends it to the in-memory
   (eventually on-chain) model registry.
3. The registry is keyed by `model_id`. Re-registration overwrites the stack
   and bumps `registered_at`; old results under the previous stack are not
   retroactively invalidated but are marked with the prior stack version.
4. **Not yet built:** on-chain registry (IPFS/calldata commit of the stack
   hash); multi-party sign-off before a stack becomes active; automatic weight
   digest verification at node registration time.

### 12.4 Node compliance

A node advertising a model must, when assigned a task for that model:

- Run the exact runtime version specified (`>= minimum_version` is acceptable
  for patch releases; minor version bumps require a new stack registration).
- Use the registered quantization format. Running `Q8_0` when the stack
  specifies `Q4_K_M` is non-compliant.
- Pass `temperature=0` and `seed=<registered_seed>` in every inference call.
- Not modify the system prompt.

**Enforcement today:** nodes are trusted to self-report compliance. The
reference stack gates *which models are eligible for the Standard tier* but
does not cryptographically verify node compliance at runtime — that requires
the Attested (TEE) tier (explicitly deferred, VERIFICATION.md).

The registered weight `digest` enables a future enrollment flow: when a node
first advertises a model, the orchestrator can request the node to return the
digest of its weights file; a mismatch ejects the node before it ever receives
a task.

### 12.5 Model eligibility

```
model_id in registry  AND  stack.temperature == 0  AND  stack.seed is set
  → eligible for RedundantExecutionVerifier (Standard tier)

model_id not in registry
  → falls back to ContentVerifier (optimistic, no recheck)
  → flagged as unverified in task record
```

A network with zero registered stacks (the current state) is fully backwards
compatible: all models run under `ContentVerifier`. Adding a stack for a model
upgrades that model to Standard-tier verification with no orchestrator restart
required — the registry lookup is per-task.

### 12.6 Open items (not designed here)

- The admin endpoint and registry persistence (in-memory today; needs a durable
  store before restarts lose registrations).
- Weight-digest enrollment flow at node registration time.
- On-chain registry commit for tamper-evidence (needed before mainnet).
- Handling runtime version ranges across heterogeneous node hardware.
- Stack versioning and migration when a model author releases an update.

---

## 13. Committee Escalation Design

> Status: **designed and implemented** in `protocol/committee.py` +
> orchestrator. A `DISPUTED` task now convenes the committee, tallies votes
> per §13.4, and on a confirmed dishonest verdict schedules a delayed slash
> (appeal window per §13.6) that reduces the in-memory bond and — in chain
> mode — the off-chain accrual bond and calls `SlashingContract.slash`.
> Conformance details in §8. Items still deferred per §13.9 (the appeal
> mechanism itself, reputation-weighted committee selection, and the
> on-chain dispute-slash bridge as a distinct path) are explicitly listed
> there.

### 13.1 When escalation triggers

Escalation is triggered by one event only: a primary/checker comparison returns
`accepted=False` from `RedundantExecutionVerifier.compare()` — i.e., the task
reaches state `DISPUTED` (§2). **No other event triggers escalation.** In
particular: a single malformed result (§5.1) goes directly to the slash path
without a committee.

### 13.2 Committee composition

The committee is a set of N independent nodes re-running the same task.

**Selection constraints:**
- Exclude the primary and the checker (they are the parties in dispute).
- Prefer nodes with no recent disputes (low dispute rate).
- Prefer nodes that have been active longest (higher earned bond — more at
  stake if they collude).
- Select uniformly at random within the eligible pool after applying the above
  filters, to prevent a predictable committee from being targeted.

**Size N:** deferred — testnet calibration required. Starting point: `N=3`
(minimum for a majority with one dissenter). Must be odd to avoid ties.
Larger committees increase cost and latency; smaller ones are more susceptible
to collusion when the network is small.

**Quorum:** simple majority (`⌊N/2⌋ + 1`). In a 3-node committee, 2/3 agree
defines ground truth.

**Tie-breaking (should not occur with odd N):** if a genuine tie occurs,
reject the result, accrue no reward for this task, and log as
`UNRESOLVABLE`. Do not slash either side — the network cannot attribute blame.

### 13.3 Re-dispatch mechanics

The orchestrator issues the same task (identical `task_id`, model, messages,
parameters) to each committee node simultaneously. Committee nodes are not told
this is an adjudication request — the dispatch is an ordinary task message.

Timeout: if fewer than `⌊N/2⌋ + 1` committee nodes return within
`COMMITTEE_TIMEOUT_S` (default 120s, configurable), fall back to `FINALIZED*`
(optimistic accept, logged unverified) rather than punishing either party for
a thin network. Record both the primary and checker node IDs as elevated-`p`
targets for future tasks.

### 13.4 Verdict and blame attribution

1. Collect all committee responses. Apply the same comparator and threshold as
   the primary/checker comparison (§4).
2. Compare each committee response to the primary result. A committee response
   is a **primary vote** if `similarity(committee_i, primary) >=
   agreement_threshold`; otherwise it is a **checker vote**.
3. Majority side wins. The dissenting node(s) — whether primary, checker, or
   a committee member — are deemed **dishonest**.

**Example (N=3):**

```
committee A agrees with primary  → primary vote
committee B agrees with primary  → primary vote
committee C agrees with checker  → checker vote
verdict: primary upheld (2/3), checker is dishonest → checker slashed
```

**False-positive protection:**
- A single disagreement between primary and checker, with no committee
  corroboration, never slashes. The committee is convened first.
- If the committee itself splits evenly (tie — should not happen with odd N),
  treat as `UNRESOLVABLE`, no slash.
- A committee member whose output matches neither primary nor checker beyond
  threshold is itself suspicious; flag it for elevated `p` but do not slash it
  from this single event alone — two independent events required before a
  committee member is slashed.

### 13.5 Slash mechanics

On a committee verdict of dishonesty:

1. **Reverse accrual:** the dishonest node's reward for this task is removed
   from the off-chain accrual ledger (it was never finalized since the task was
   in `DISPUTED`).
2. **Slash bond:** deduct `slash_fraction` (deferred, testnet) of the
   dishonest node's **cumulative unvested accrual** — the same bond described
   in ECONOMICS.md §4. This is earned income held in escrow, not capital the
   node had to post upfront (the no-barrier-to-entry property is preserved).
3. **Burn:** slashed tokens are sent to the burn sink (ECONOMICS.md §2). They
   are not redistributed to the winning party to avoid perverse checker
   incentives.
4. **Eject if bond exhausted:** if the slash reduces the node's unvested
   accrual to zero or below, eject the node (same path as `SlashingContract`).
5. **Compensate checker/committee (deferred):** the economics of verification
   incentive payments are an open item (ECONOMICS.md §4–§5). Today, checkers
   and committee nodes earn nothing; the routing fix ensures they are not
   permanently relegated to unpaid verification (slots alternate fairly).

### 13.6 Appeal window

Before a slash is applied as irreversible:

1. The verdict is logged with a timestamp.
2. An **appeal window** of `APPEAL_WINDOW_S` (deferred, testnet — starting
   point: 3600s / 1 hour) elapses during which the node can contest.
3. **Appeal mechanism (not yet designed):** the appeal path requires a
   governance or oracle mechanism not yet built. For now, the appeal window is
   a time-lock only — it delays the slash but does not create a real appeal
   path. The slash fires automatically when the window expires.
4. During the appeal window, the node continues operating (it is not
   pre-emptively ejected) but its `p` is elevated so subsequent tasks are
   checked at higher frequency.

### 13.7 Escalation state flow (extends §2)

```
DISPUTED
  └─> COMMITTEE_DISPATCHED    orchestrator: select N nodes, dispatch same task
        └─> COMMITTEE_WAITING (until ⌊N/2⌋+1 responses or timeout)
              ├─ timeout ─> FINALIZED* (optimistic, unverified, elevated-p)
              └─> COMMITTEE_VERDICT
                    ├─ primary upheld ─> FINALIZED (primary reward accrues)
                    │                    checker: elevated-p, reverse-its-accrual
                    ├─ checker upheld ─> APPEAL_PENDING (primary: elevated-p,
                    │                    slash queued after APPEAL_WINDOW_S)
                    └─> tie            ─> UNRESOLVABLE (no reward, no slash)
```

`APPEAL_PENDING → SLASHED` fires after `APPEAL_WINDOW_S` with no successful
appeal. `SLASHED` executes steps 1–5 of §13.5.

### 13.8 Parameters

| Parameter | Meaning | Status | Default |
|---|---|---|---|
| `N` | Committee size | deferred, testnet | 3 (odd) |
| `quorum` | Votes needed for majority | derived: `⌊N/2⌋ + 1` | 2 |
| `COMMITTEE_TIMEOUT_S` | Max wait for committee responses | empirical | 120s |
| `APPEAL_WINDOW_S` | Delay before slash is irreversible | empirical | 3600s |
| `slash_fraction` | Fraction of unvested bond burned | deferred (ECONOMICS.md §5) | TBD |

### 13.9 What is not designed here

- The appeal mechanism proper (requires governance or a trusted oracle not yet
  built).
- Collusion-resistant committee selection when the attacker controls a large
  share of the node pool (explicitly deferred as an open problem in §6).
- Committee member compensation economics (deferred to ECONOMICS.md §4–§5).
- The on-chain slash transaction path for committee verdicts (currently only
  `SlashingContract.slash()` exists — the committee verdict must be bridged to
  it, requiring a new contract call or relayer design).

---

## 14. Decided vs. deferred

- **Decided:** optimistic redundant execution is the Standard tier for
  Stage 0–1; silent sampling; recheck on a different node; tolerant pluggable
  comparison; two-sample disagreement never auto-slashes; slashing requires
  committee majority (§13) and is taken from the unvested earned bond and
  burned; reference inference stack is a hard prerequisite for a model to
  enter the Standard tier (§12); verification work-measurement is the same
  substrate the redemption rate consumes (one keystone); semantic embedding
  cosine similarity is the Standard-tier default comparator (§4).
- **Deferred (empirical, testnet — more than one attempt expected):** `p` and
  its conditioning; comparison threshold `T`; committee size `N` and quorum;
  `COMMITTEE_TIMEOUT_S`; `APPEAL_WINDOW_S`; slash magnitude (`slash_fraction`);
  collusion-resistant checker/committee selection; checker/committee compensation.
- **Deferred to later stages:** Attested/Proven tiers; Stage 2 sharded
attribution; replacing the single trusted orchestrator; on-chain model
  registry; appeal mechanism proper (governance/oracle).
attribution; replacing the single trusted orchestrator.
- **Decided (principles, §9–§10):** verification certifies *honest execution
  of the chosen model*, not absolute answer quality; token count is a billing
  unit, not a quality signal; specs are the wrong target — verify delivered
  work, and only TEE attests hardware; redundancy is a sampled tax, not the
  operating mode — the network shares work, it never multiplies it.
- **Research / planned (§9, §11):** pre-flight qualification (golden
  challenges → measured tier); canary / known-answer tasks; the cheap
  heuristic floor (truncation/repetition/format); the verification pool
  (judge tier) as the committee implementation and a premium assurance lane.