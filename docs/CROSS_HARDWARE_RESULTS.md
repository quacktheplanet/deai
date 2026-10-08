# Cross-hardware agreement and the first live committee — results

> Run 2026-10-08 on one machine (RTX 5090 + Xeon Gold 6530), everything bound to
> 127.0.0.1, mock ledger only, nothing on-chain. Raw data: `docs/data/cross_hardware_2026-10-08/`.
> Script: `tests/cross_backend_agreement.py`.

## Why

Every earlier agreement test ran two nodes through the same Ollama on the same GPU, so
they agreed almost perfectly. A real network has honest nodes on different hardware. Two
questions had to be answered before slashing can ever be switched on:

1. How far apart are two **honest** nodes running the same model with the same settings on
   different hardware? The agreement threshold must sit below that, or honest providers
   get slashed.
2. How close does a **cheating** node get? The threshold must sit above that, or cheating
   pays.

## Setup

Same model file (Qwen2.5-7B-Instruct, Q4_K_M GGUF) on three honest "machines", all
llama.cpp `llama-server`, temperature 0, seed 42, the exact request a node sends:

| Backend | Hardware |
| --- | --- |
| `gpu` | everything on the RTX 5090 |
| `cpu` | CPU only, 8 threads, no GPU |
| `gpu+cpu` | 10 of 28 layers on the GPU, the rest on the CPU (a small-GPU home machine) |

Two kinds of cheating were compared against them:

- **small** — a much smaller model (Qwen2.5-0.5B) answering instead of the real one (the
  cheap-substitute cheat: same work claimed, ~1/14 of the compute spent);
- **swapped** — a real answer, but to a different question.

30 prompts: the 15 from `tests/ollama_agreement_test.py` plus 15 longer ones (essays, code,
lists); every honest backend answered each twice. Pairs were scored with the orchestrator's
own comparators: embedding cosine (nomic-embed-text v1.5) and sequence ratio.

The planned 14B run on Ollama was replaced by 7B on llama-server because the pod's network
volume (`/workspace`, where Ollama keeps its models) hung during the run; see *Notes*.

## Results

| Pairs | n | Same text exactly | Embedding min / 5th pct / median | Below 0.85 |
| --- | --- | --- | --- | --- |
| Same backend, run 1 vs run 2 (honest) | 90 | 59 | 0.844 / 0.954 / 1.000 | 1 |
| Different hardware (honest) | 180 | 71 | 0.790 / 0.923 / 0.999 | 5 |
| Small-model substitute (cheat) | 30 | 2 | 0.624 / 0.689 / 0.921 | 6 |
| Answer to another question (cheat) | 30 | 0 | 0.244 / 0.274 / 0.443 (max 0.505) | 30 |

Threshold sweep (honest pairs wrongly below / cheats wrongly above):

| Threshold | Honest flagged (of 270) | Small model passes (of 30) | Swapped passes (of 30) |
| --- | --- | --- | --- |
| 0.70 | 0 | 28 | 0 |
| 0.75 | 0 | 28 | 0 |
| 0.80 | 2 | 27 | 0 |
| **0.85 (current)** | **6 (2.2%)** | **24** | 0 |
| 0.90 | 6 | 18 | 0 |
| 0.95 | 19 | 7 | 0 |

What this says, plainly:

- **Different hardware does change the words.** Only 71 of 180 honest cross-hardware pairs
  were identical. Greedy decoding drifts once one token differs, and the GPU isn't even
  consistent with itself: the GPU backend repeated its own answer exactly only 16 of 30
  times, the CPU-only backend 30 of 30. The meaning stays the same (median 0.999), but the
  worst honest pair scored 0.790 — two different, equally correct robot stories.
- **At today's 0.85, about 1 honest check in 45 would be a false mismatch.** Each goes to a
  committee (which, below, decided correctly), but at network scale that is a steady
  stream of committees, and any committee error lands on an honest node.
- **The embedding comparator cannot catch a cheap substitute model.** A 0.5B model's answer
  means roughly the same thing as the 7B's on most prompts (median 0.921). No threshold
  separates them: every threshold that catches most substitutes also flags honest nodes.
  Sequence ratio doesn't separate them either (honest cross-hardware pairs go as low as
  0.057). Neither does the length of the identical opening (honest pairs sometimes
  diverge in the first few characters).
- **It reliably catches wrong answers.** Answers to a different question never scored
  above 0.505, far below every honest pair.

## The committee, live, for the first time

Orchestrator with `--verify-sample-rate 1.0`, the embedding comparator, appeal window 0,
and five nodes: two on `gpu`, one on `gpu+cpu`, one on `cpu`, plus one cheater. The
reference stack was registered by the agreement test itself.

**A node returning junk** (the mock node: canned text instead of inference): it was the
primary 13 times, mismatched every time (agreement 0.43–0.76), and every one of the 13
committees voted 3–0 against it. It was slashed 13 times; **no honest node was ever
slashed**. Log: `docs/data/cross_hardware_2026-10-08/committee_log.txt`.

**A node serving the 0.5B model under the 7B's name:** it took part in 7 of 15 tasks
(as primary or checker) and was caught **once** (agreement 0.803 → committee 3–0 → slashed).
The other 6 passed at 0.899–1.000, and it earned 31 DAI against ~41–51 for the honest
nodes — the comparator limit above, reproduced live.

Two problems the live run exposed (not fixed here):

1. **A caught cheater gets all the primary traffic.** Routing prefers the node idle
   longest. A mismatch makes the checker and three committee members busy, so their idle
   clocks reset and the cheater's doesn't — it was chosen as primary for 13 requests in a
   row. Any node with an open dispute (or a recent slash) should be skipped or deprioritized.
2. **The requester gets a 502 even when the committee found the right answer.** On
   `CHECKER_UPHELD` the checker's answer, confirmed 3–0, could be returned instead of an
   error. (Also: the mismatch log line still says "committee escalation required (not yet
   implemented)", which is out of date.)

## Recommendation

- **Threshold: lower it to 0.75.** In this data that is zero honest false mismatches across
  three kinds of hardware, with a wide margin (0.25) above the highest wrong-question
  answer. Treat 0.75 as the bootstrap value and keep collecting cross-hardware data
  (different GPUs, Apple Silicon, other quantizations) before slashing is real.
- **Be honest about what the embedding check verifies:** "this is an answer to this
  question", not "this came from the claimed model". Catching a cheaper-model substitute
  needs a different signal. The standard one is a **likelihood check**: the checker scores
  the primary's exact tokens under its own copy of the model (one forward pass, no
  generation — cheaper than re-running the task). A substitute model's text is far less
  likely under the real model than an honest node's, even when the meaning matches.
  `llama-server` can return per-token log-probabilities, so this is buildable with the
  current stack.
- **Pin more of the reference stack.** The GPU disagreeing with itself one run in two
  comes from batch and cache effects; registering `cache_prompt: false` and a single slot
  in the stack would make same-hardware repeats deterministic and shrink the honest noise
  the threshold has to absorb.
- Fix the two committee problems above before running with more than a handful of nodes.

## Notes

- The CPU-only backend was capped by the container's CPU quota (13.6 CPUs), so it ran with
  8 threads; this doesn't affect the numbers, only the speed (median 9 s per answer vs
  1.9 s on the GPU).
- Ollama itself is a llama.cpp `llama-server` underneath (it launches one per model), so
  the `gpu` backend here is the same engine an Ollama node runs.
- Rerun: start servers on each backend, then
  `python tests/cross_backend_agreement.py --backend gpu=URL --backend cpu=URL ... --model M
  --small M_SMALL@URL --embedding-url URL --runs 2 --out DIR` (answers are cached in DIR so
  an interrupted run resumes).

## Likelihood check (added 2026-10-08, branch `likelihood-check`)

The embedding check can't tell a cheaper model's answer from the real one's. So the saved answers
above were scored a second way: the reference model (Qwen2.5-7B Q4_K_M, llama.cpp b11057 on CPU)
was made to produce each answer's exact text (a grammar that admits only that string), and for each
token we read whether it was the model's own first choice and how likely it found it.

| Answers | n | First-choice share, min / median | Mean log-prob, min / median | Judged (16+ tokens) |
|---|---|---|---|---|
| Honest 7B (made by Ollama's llama.cpp build, scored by b11057) | 30 | 0.896 / 0.978 | -0.569 / -0.166 | 25 likely, **0 unlikely**, 5 too short |
| 0.5B posing as 7B | 30 | 0.261 / 0.696 | -7.59 / -1.58 | **23 unlikely**, 2 likely, 5 too short |

- With "at least 88% first choices and a mean of -0.65 or better", 23 of the 25 judgeable substitute
  answers are caught (the embedding check caught 6 of 30) and no honest answer is flagged.
- The two it misses are answers with essentially one way to say them: the list of planets and a long
  worked compound-interest example. Answers under 16 tokens ("Paris", "56") carry too little signal and
  are left to the embedding check.
- The margins are thin (honest minimum 0.896 against the 0.88 line; -0.569 against -0.65). Scoring on
  other hardware will move them; collect more before any of this can lead to a slash.
- Cost: one forced pass over the answer on the checker, at generation speed, instead of generating a
  second answer and embedding both.

How it's wired (`protocol/likelihood.py`, `--verify-likelihood`): on a recheck, a node whose backend can
score (llama-server) scores the primary's text. "Likely" verifies the task with no second generation. An
objection asks a panel of other scorers; a majority "unlikely" treats the primary as dishonest (slash
scheduled, quarantined) and the requester gets a fresh answer from another node. Anything inconclusive
falls back to generate-and-compare. Data: `docs/data/likelihood_2026-10-08/scores.json`; script:
`tests/likelihood_probe.py`.

## Reproducible answers on one machine (added 2026-10-08, branch `deterministic-answers`)

Temperature 0 and a fixed seed don't make llama-server repeat itself on a GPU. The server
batches concurrent requests from its slots together and reuses cached prompt prefixes, and
both change the shape of the GPU arithmetic, so near-ties in the logits break differently.
Measured on the RTX 5090 with Qwen2.5-0.5B: the same seeded 300-token request was repeated
6 times while 0–3 other requests ran alongside it.

| llama-server | request | distinct answers in 6 |
| --- | --- | --- |
| `-np 4` (several slots) | default | 6 |
| `-np 4` | `cache_prompt: false` | 4 |
| `-np 1` (one slot) | default | 3 |
| `-np 1` | `cache_prompt: false` | **1** (byte-identical) |

On the CPU, all four settings repeated exactly. AVE saw the same effect at scale: two
default-mode runs of a 480-case agent benchmark disagreed on 7.5% of cases, and two runs
with one slot and no prompt cache matched byte for byte (ave-registry
`benchmark/RESULTS-v3.md`).

So on this branch:

- A node whose backend is llama-server sends `cache_prompt: false` with every seeded task.
  Seeded means the model has a registered stack, so the answer is meant to be checkable.
- At start-up the node reads the server's slot count (`/props`) and, if it's above 1, says
  to start llama-server with `-np 1` for answers that repeat exactly.
- The verifier accepts identical text as a match before asking the embedding comparator.
  Two such nodes on the same hardware then verify each other exactly, with no model call
  and no threshold. Across different hardware, answers still differ slightly (sections
  above), and the comparator or the likelihood check takes over as before.

A single slot serves one request at a time. That costs throughput on a busy node, which is
why it's a recommendation and not enforced.
