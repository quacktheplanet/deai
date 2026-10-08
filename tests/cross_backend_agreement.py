"""
Cross-backend agreement measurement (no orchestrator needed).

How far apart are two HONEST nodes running the same model with the same
settings on different hardware / inference engines? The agreement threshold
has to sit below that, and above what a cheating node produces, before
slashing can ever be turned on (docs/VERIFICATION_PROTOCOL.md §4, §7).

For every prompt it asks each backend `--runs` times (temperature 0, seed 42,
the same request a node sends), then scores pairs with the orchestrator's own
comparators (embedding cosine and sequence ratio):

  repeat      the same backend, run i vs run j       (honest)
  cross       backend X vs backend Y, run 0          (honest)
  small       an honest answer vs a much smaller model's answer   (dishonest)
  swapped     an honest answer vs the answer to a different prompt (dishonest)

Usage:
    python tests/cross_backend_agreement.py \\
        --backend ollama-gpu=http://localhost:11434 \\
        --backend llama-cpu=http://127.0.0.1:18081 \\
        --model qwen2.5:14b --small qwen2.5:0.5b@http://localhost:11434 \\
        --embedding-url http://localhost:11434 --out results/xhw

Answers are cached in --out/answers.json, so a rerun only scores.
"""

import argparse
import itertools
import json
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "protocol"))
sys.path.insert(0, HERE)
from verification import EmbeddingComparator, default_comparator  # noqa: E402
from ollama_agreement_test import PROMPTS as BASE  # noqa: E402

EXTRA = {
    "long": [
        ("photosynthesis", "Explain photosynthesis to a high-school student in about 200 words."),
        ("french revolution", "Summarize the causes of the French Revolution in three short paragraphs."),
        ("CAP theorem", "Explain the CAP theorem and give one real database as an example of each trade-off."),
        ("vaccines", "How do mRNA vaccines work? Answer in about 150 words."),
        ("compound interest", "Explain compound interest with a worked example using $1,000 at 5% for 3 years."),
    ],
    "code": [
        ("fizzbuzz", "Write a Python function fizzbuzz(n) that returns the FizzBuzz list from 1 to n. Code only."),
        ("reverse list", "Write a C function that reverses a singly linked list in place. Code only, with a short comment."),
        ("sql top", "Write a SQL query returning the 5 customers with the highest total order amount from tables customers(id, name) and orders(id, customer_id, amount)."),
        ("regex email", "Give a regular expression that loosely validates an email address, and explain each part briefly."),
        ("binary search", "Implement binary search in JavaScript and state its time complexity."),
    ],
    "list": [
        ("planets", "List the planets of the solar system in order from the Sun, one per line."),
        ("git commands", "List ten common git commands with a one-line description each."),
        ("healthy breakfast", "Give five ideas for a healthy breakfast, as a bulleted list."),
        ("prime numbers", "List the first fifteen prime numbers, comma separated."),
        ("interview questions", "List six good questions to ask at the end of a job interview."),
    ],
}


def prompts():
    out = []
    for cat, items in BASE.items():
        out += [(cat, name, msgs) for name, msgs in items]
    for cat, items in EXTRA.items():
        out += [(cat, name, [{"role": "user", "content": text}]) for name, text in items]
    return out


def ask(url: str, model: str, messages: list, timeout: float) -> dict:
    """The request compute/node.py sends for a task with a registered stack."""
    body = {"model": model, "messages": messages, "max_tokens": 2048, "temperature": 0.0,
            "stream": False, "think": False, "seed": 42}
    t = time.time()
    r = httpx.post(url.rstrip("/") + "/v1/chat/completions", json=body, timeout=timeout)
    r.raise_for_status()
    return {"text": r.json()["choices"][0]["message"]["content"] or "", "seconds": time.time() - t}


def collect(args, items, path):
    answers = json.load(open(path)) if os.path.exists(path) else {}
    backends = dict(b.split("=", 1) for b in args.backend)
    if args.small:
        model, url = args.small.split("@", 1)
        backends["small"] = url
    jobs = []
    for cat, name, msgs in items:
        for b, url in backends.items():
            runs = 1 if b == "small" else args.runs
            for i in range(runs):
                key = f"{b}|{name}|{i}"
                if key not in answers:
                    jobs.append((key, url, model if b == "small" else args.model, msgs))

    lock = threading.Lock()

    def run(job):
        key, url, model, msgs = job
        try:
            a = ask(url, model, msgs, args.timeout)
        except Exception as e:
            a = {"error": f"{type(e).__name__}: {e}"}
        with lock:  # saved as it goes, so an interrupted run resumes
            answers[key] = a
            json.dump(answers, open(path, "w"), indent=1)
        return key, a

    # One worker per backend: each server handles one request at a time, and
    # parallel requests to the same server would change its batching.
    by_url = {}
    for j in jobs:
        by_url.setdefault(j[1], []).append(j)
    done = 0
    with ThreadPoolExecutor(max_workers=len(by_url) or 1) as pool:
        for chunk in pool.map(lambda js: [run(j) for j in js], by_url.values()):
            done += len(chunk)
    print(f"collected {done} new answers ({len(answers)} total)", flush=True)
    return answers, [b for b in backends if b != "small"]


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, round(p / 100 * (len(xs) - 1))))]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", action="append", required=True, help="name=base_url (OpenAI-compatible)")
    ap.add_argument("--model", required=True)
    ap.add_argument("--small", help="model@base_url for the dishonest small-model baseline")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--embedding-url", required=True)
    ap.add_argument("--embedding-model", default="nomic-embed-text")
    ap.add_argument("--threshold", type=float, default=0.85)
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--out", default="xhw_results")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    items = prompts()
    answers, honest = collect(args, items, os.path.join(args.out, "answers.json"))
    emb = EmbeddingComparator(args.embedding_url, args.embedding_model, timeout=60)

    def text(b, name, i):
        a = answers.get(f"{b}|{name}|{i}", {})
        return a.get("text") if a.get("text") else None

    pairs = []  # (kind, label, a, b)
    names = [n for _, n, _ in items]
    for _, name, _ in items:
        for b in honest:
            for i, j in itertools.combinations(range(args.runs), 2):
                pairs.append(("repeat", f"{b} {name} run{i}/run{j}", text(b, name, i), text(b, name, j)))
        for x, y in itertools.combinations(honest, 2):
            for i in range(args.runs):
                pairs.append(("cross", f"{x}~{y} {name} run{i}", text(x, name, i), text(y, name, i)))
        if args.small:
            pairs.append(("small", f"{honest[0]}~small {name}", text(honest[0], name, 0), text("small", name, 0)))
        other = names[(names.index(name) + 1) % len(names)]
        pairs.append(("swapped", f"{name} vs answer to {other}", text(honest[0], name, 0), text(honest[0], other, 0)))

    rows = []
    for kind, label, a, b in pairs:
        if a is None or b is None:
            continue
        rows.append({"kind": kind, "label": label, "identical": a == b,
                     "embedding": emb(a, b), "sequence": default_comparator(a, b)})
    json.dump(rows, open(os.path.join(args.out, "scores.json"), "w"), indent=1)

    errors = {k: v["error"] for k, v in answers.items() if "error" in v}
    secs = {}
    for k, v in answers.items():
        if "seconds" in v:
            secs.setdefault(k.split("|")[0], []).append(v["seconds"])

    print(f"\nthreshold {args.threshold}   prompts {len(items)}   runs {args.runs}   errors {len(errors)}")
    for b, s in secs.items():
        print(f"  {b:<12} median {statistics.median(s):5.1f}s per answer, max {max(s):5.1f}s")
    print(f"\n{'pairs':<9}{'n':>4}{'same text':>11}  {'embedding min / p5 / median':>28}  {'< thr':>6}  {'sequence min / median':>22}")
    for kind in ("repeat", "cross", "small", "swapped"):
        rs = [r for r in rows if r["kind"] == kind]
        if not rs:
            continue
        e = [r["embedding"] for r in rs]
        s = [r["sequence"] for r in rs]
        below = sum(x < args.threshold for x in e)
        print(f"{kind:<9}{len(rs):>4}{sum(r['identical'] for r in rs):>11}  "
              f"{min(e):>8.3f} / {pct(e, 5):.3f} / {statistics.median(e):.3f}  "
              f"{below:>6}  {min(s):>10.3f} / {statistics.median(s):.3f}")
    honest_rows = [r for r in rows if r["kind"] in ("repeat", "cross")]
    bad_rows = [r for r in rows if r["kind"] in ("small", "swapped")]
    if honest_rows and bad_rows:
        lo = min(r["embedding"] for r in honest_rows)
        hi = max(r["embedding"] for r in bad_rows)
        print(f"\nlowest honest {lo:.3f}   highest dishonest {hi:.3f}   gap {lo - hi:+.3f}")
        print("worst honest pairs:")
        for r in sorted(honest_rows, key=lambda r: r["embedding"])[:5]:
            print(f"  {r['embedding']:.3f}  {r['label']}")
        print("best-scoring dishonest pairs:")
        for r in sorted(bad_rows, key=lambda r: -r["embedding"])[:5]:
            print(f"  {r['embedding']:.3f}  {r['label']}")
    for k, v in list(errors.items())[:10]:
        print("ERROR", k, v)


if __name__ == "__main__":
    main()
