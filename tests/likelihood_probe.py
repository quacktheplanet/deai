"""
Likelihood-check measurement (docs/CROSS_HARDWARE_RESULTS.md, "Likelihood check").

Scores the saved honest (cpu) and substitute (small) answers from the
cross-hardware run under the reference model, and prints both distributions.
Needs a llama-server with the reference model (Qwen2.5-7B-Instruct Q4_K_M):

    llama-server -m qwen2.5-7b-instruct-q4_k_m.gguf --port 18100 -np 1 [--device none]
    python tests/likelihood_probe.py [--url http://127.0.0.1:18100]

Resumes from the scores file if interrupted.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from cross_backend_agreement import prompts  # noqa: E402
from protocol.likelihood import LikelihoodPolicy, score_text  # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), "docs", "data")
ANSWERS = os.path.join(DATA, "cross_hardware_2026-10-08", "answers.json")
OUT = os.path.join(DATA, "likelihood_2026-10-08", "scores.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:18100")
    args = ap.parse_args()
    answers = json.load(open(ANSWERS))
    done = json.load(open(OUT)) if os.path.exists(OUT) else {}
    items = {name: msgs for _, name, msgs in prompts()}
    for key in [k for k in answers if k.split("|")[0] in ("cpu", "small") and k.endswith("|0")]:
        if key in done:
            continue
        s = asyncio.run(score_text(args.url, items[key.split("|")[1]], answers[key]["text"]))
        done[key] = {"tokens": s.tokens, "top1": s.top1_rate, "mean_lp": s.mean_logprob} if s else None
        print(key, done[key], flush=True)
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        json.dump(done, open(OUT, "w"), indent=1)

    policy = LikelihoodPolicy()
    for who in ("cpu", "small"):
        rows = [v for k, v in done.items() if k.startswith(who + "|") and v]
        judged = [policy.judge(type("S", (), {"tokens": r["tokens"], "top1_rate": r["top1"],
                                              "mean_logprob": r["mean_lp"]})()) for r in rows]
        print(f"{who:<6} n={len(rows)}  top1 min/median {min(r['top1'] for r in rows):.3f}/"
              f"{statistics.median(r['top1'] for r in rows):.3f}  mean_lp min/median "
              f"{min(r['mean_lp'] for r in rows):.3f}/{statistics.median(r['mean_lp'] for r in rows):.3f}  "
              f"likely {judged.count(True)}  unlikely {judged.count(False)}  inconclusive {judged.count(None)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
