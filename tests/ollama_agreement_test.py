"""
Two-node Ollama agreement test.

Fires a batch of prompts across three categories (factual, explanatory,
creative/ambiguous) with --verify-sample-rate 1.0 active on the orchestrator.
Reports per-category pass rate. Agreement scores appear in the orchestrator
logs (look for "VERIFY OK" / "VERIFY MISMATCH" lines).

A row only passes if the orchestrator really compared two nodes' answers
(X-DAI-Verification: verified). Before the run the script registers a
reference stack for the model if it has none — without one the orchestrator
silently skips redundant verification and every row would "pass" unchecked.
A 200 that was unchecked or unverified (no free checker, comparator down)
counts as a failure.

Usage:
    # Terminal 1 — orchestrator (embedding comparator on)
    python protocol/orchestrator.py \\
        --verify-sample-rate 1.0 \\
        --verify-threshold 0.85 \\
        --embedding-url http://localhost:11434

    # Terminals 2 & 3 — two real Ollama nodes
    python compute/node.py --ollama --models qwen3:8b
    python compute/node.py --ollama --models qwen3:8b

    # Terminal 4 — this script
    python tests/ollama_agreement_test.py
    python tests/ollama_agreement_test.py --threshold 0.80   # if 0.85 is too tight
    python tests/ollama_agreement_test.py --model llama3
    python tests/ollama_agreement_test.py --no-register   # test an existing stack only
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

# ── Prompt bank ──────────────────────────────────────────────────────────────

PROMPTS = {
    "factual": [
        ("capital of France",
         [{"role": "user", "content": "What is the capital of France? Answer in one word."}]),
        ("7 × 8",
         [{"role": "user", "content": "What is 7 multiplied by 8? Answer with just the number."}]),
        ("WW2 end year",
         [{"role": "user", "content": "What year did World War II end? Answer with just the year."}]),
        ("boiling point water",
         [{"role": "user", "content": "What is the boiling point of water in Celsius? Answer with just the number."}]),
        ("speed of light unit",
         [{"role": "user", "content": "What unit is the speed of light measured in? Answer in three words or fewer."}]),
    ],
    "explanatory": [
        ("TCP handshake",
         [{"role": "user", "content": "Explain how a TCP three-way handshake works. Keep it under 100 words."}]),
        ("list vs tuple",
         [{"role": "user", "content": "What are the main differences between Python lists and tuples? Keep it under 80 words."}]),
        ("HTTPS encryption",
         [{"role": "user", "content": "Describe how HTTPS encrypts web traffic. Keep it under 100 words."}]),
        ("RAM vs storage",
         [{"role": "user", "content": "What is the difference between RAM and storage? Keep it under 80 words."}]),
        ("git rebase vs merge",
         [{"role": "user", "content": "What is the difference between git rebase and git merge? Keep it under 80 words."}]),
    ],
    "creative": [
        ("robot story",
         [{"role": "user", "content": "Write exactly two sentences: a short story about a robot who learns to cook."}]),
        ("meaning of life",
         [{"role": "user", "content": "What is the meaning of life? Answer in one sentence."}]),
        ("productivity tips",
         [{"role": "user", "content": "Give exactly three short bullet points: tips for staying focused while coding."}]),
        ("describe blue",
         [{"role": "user", "content": "Describe the color blue to someone who has never seen it. One sentence only."}]),
        ("haiku about Python",
         [{"role": "user", "content": "Write a haiku about the Python programming language."}]),
    ],
}


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _request(url: str, body: dict | None, timeout: float, api_key: str | None,
             method: str | None = None):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method or ("POST" if body is not None else "GET"),
    )
    return urllib.request.urlopen(req, timeout=timeout)


def post_chat(url: str, model: str, messages: list, timeout: int,
              api_key: str | None = None) -> tuple[int, str, str]:
    """Returns (status code, text, verification) where verification is the
    orchestrator's X-DAI-Verification header (verified/unverified/unchecked)."""
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": 512,
        "temperature": 0.0,
    }
    try:
        with _request(url, body, timeout, api_key) as resp:
            data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"]
            return 200, content, resp.headers.get("X-DAI-Verification", "")
    except urllib.error.HTTPError as e:
        return e.code, e.reason, ""
    except Exception as e:
        return 0, str(e), ""


def shared_model(base: str, api_key: str | None) -> str | None:
    """A model at least two connected nodes advertise (a recheck needs a
    second node), or None."""
    with _request(base + "/status", None, 10, api_key) as resp:
        status = json.loads(resp.read())
    counts: dict[str, int] = {}
    for node in status.get("nodes", []):
        for m in set(node.get("models", [])):
            if m != "any" and "embed" not in m.lower():
                counts[m] = counts.get(m, 0) + 1
    shared = sorted((m for m, n in counts.items() if n >= 2), key=lambda m: -counts[m])
    return shared[0] if shared else None


def ensure_registered(base: str, model: str, api_key: str | None, register: bool) -> bool:
    """True when the model has a reference stack (registering one if allowed)."""
    try:
        with _request(f"{base}/admin/model-registry/{model}", None, 10, api_key) as resp:
            stack = json.loads(resp.read())
            print(f"  stack        : registered (seed {stack.get('seed')}, temperature {stack.get('temperature')})")
            return True
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"  stack        : could not check the registry ({e.code} {e.reason})")
            return False
    if not register:
        print("  stack        : NONE — the orchestrator will not recheck this model (--no-register given)")
        return False
    body = {"model_id": model, "runtime": "ollama", "temperature": 0.0, "seed": 42,
            "max_tokens": 2048, "registered_by": "ollama_agreement_test"}
    try:
        with _request(f"{base}/admin/model-registry", body, 10, api_key):
            print("  stack        : registered now (seed 42, temperature 0)")
            return True
    except urllib.error.HTTPError as e:
        print(f"  stack        : registration failed ({e.code} {e.reason})")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def run(args):
    base = args.orchestrator.rstrip("/")
    url = base + "/v1/chat/completions"
    delay = args.delay

    if args.model == "any":
        # Verification is per registered model; "any" never has a stack.
        try:
            model = shared_model(base, args.api_key)
        except Exception as e:
            print(f"Orchestrator unreachable at {base}: {e}")
            return 1
        if model is None:
            print("No model is served by two connected nodes; start two "
                  "`node.py --ollama --models <model>` clients first.")
            return 1
        args.model = model

    results = {}  # category -> list of (name, kind); kind is pass | fail | unver | err

    print(f"\nDAI two-node Ollama agreement test")
    print(f"  orchestrator : {args.orchestrator}")
    print(f"  model        : {args.model}")
    print(f"  threshold    : {args.threshold}  (set on orchestrator — not enforced here)")
    print(f"  timeout/req  : {args.timeout}s")
    if not ensure_registered(base, args.model, args.api_key, not args.no_register):
        print("\nWithout a registered stack nothing would be verified; stopping.\n")
        return 1
    print()
    print("  Scores appear in orchestrator logs: grep 'VERIFY' orchestrator output\n")
    print(f"{'Category':<14} {'Prompt':<22} {'Status':>6}  {'Result'}")
    print("-" * 70)

    total_pass = total_fail = total_err = total_unver = 0

    for category, prompts in PROMPTS.items():
        cat_results = []
        for name, messages in prompts:
            code, text, verification = post_chat(url, args.model, messages, args.timeout, args.api_key)

            if code == 200 and verification == "verified":
                status_str = "200 OK"
                passed = True
                total_pass += 1
                detail = text[:60].replace("\n", " ") + ("..." if len(text) > 60 else "")
            elif code == 200:
                status_str = f"200 {(verification or 'no header').upper()}"
                passed = False
                total_unver += 1
                detail = {
                    "unverified": "(accepted without a comparison — no free checker or comparator down)",
                    "unchecked": "(not rechecked — is --verify-sample-rate 1.0 set?)",
                }.get(verification, "(orchestrator predates the X-DAI-Verification header)")
            elif code == 502:
                status_str = "502 MISMATCH"
                passed = False
                total_fail += 1
                detail = "(nodes disagreed — see orchestrator logs)"
            elif code == 503:
                status_str = "503 NO NODE"
                passed = False
                total_err += 1
                detail = "(no node available)"
            elif code == 504:
                status_str = "504 TIMEOUT"
                passed = False
                total_err += 1
                detail = "(node timed out)"
            else:
                status_str = f"{code} ERR"
                passed = False
                total_err += 1
                detail = text[:60]

            mark = "✓" if passed else "✗"
            print(f"{category:<14} {name:<22} {status_str:>12}  {mark}  {detail}")
            kind = "pass" if passed else "unver" if code == 200 else "fail" if code == 502 else "err"
            cat_results.append((name, kind))

            if delay > 0:
                time.sleep(delay)

        results[category] = cat_results
        print()

    # Per-category summary
    print("=" * 70)
    print(f"{'Category':<14}  {'Pass':>4}  {'Fail':>4}  {'Unver':>5}  {'Err':>4}  {'Rate':>6}")
    print("-" * 70)
    for category, cat_results in results.items():
        p, f, u, e = (sum(1 for _, k in cat_results if k == kind) for kind in ("pass", "fail", "unver", "err"))
        n = len(cat_results)
        rate = f"{p/n*100:.0f}%" if n else "-"
        print(f"{category:<14}  {p:>4}  {f:>4}  {u:>5}  {e:>4}  {rate:>6}")
    print("-" * 70)
    total = total_pass + total_fail + total_unver + total_err
    rate = f"{total_pass/total*100:.0f}%" if total else "-"
    print(f"{'TOTAL':<14}  {total_pass:>4}  {total_fail:>4}  {total_unver:>5}  {total_err:>4}  {rate:>6}")
    print()

    if total_unver > 0:
        print("UNVER rows: answered, but the two results were never compared, so the")
        print("row proves nothing. See the orchestrator log ('VERIFY unverified' /")
        print("'VERIFY skip') for why.\n")

    if total_err > 0:
        print("ERR rows = orchestrator unreachable or no nodes connected.")
        print("Make sure two `node.py --ollama` clients are running.\n")

    if total_fail > 0:
        print("MISMATCH rows (502): two nodes returned semantically different answers.")
        print("Check orchestrator logs for agreement scores — the threshold may need")
        print(f"lowering (current: {args.threshold}). Re-run with --threshold 0.75 to see")
        print("if the score is close to the boundary.\n")

    return 0 if total_err == 0 and total_fail == 0 and total_unver == 0 else 1


def parse_args():
    p = argparse.ArgumentParser(description="Two-node Ollama agreement stress test")
    p.add_argument("--orchestrator", default="http://localhost:8000",
                   help="Orchestrator base URL (default: http://localhost:8000)")
    p.add_argument("--model", default="any",
                   help="Model to request (default: any = one that two connected nodes serve)")
    p.add_argument("--no-register", action="store_true",
                   help="Don't register a reference stack for the model if it has none")
    p.add_argument("--api-key", default=os.getenv("DAI_API_KEY"),
                   help="Orchestrator API key, if it requires one")
    p.add_argument("--threshold", type=float, default=0.85,
                   help="Threshold set on orchestrator — printed for reference only (default: 0.85)")
    p.add_argument("--timeout", type=int, default=120,
                   help="Per-request timeout in seconds (default: 120)")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Seconds between requests (default: 1.0)")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
