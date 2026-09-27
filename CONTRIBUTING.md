# Contributing to DAI

Thanks for your interest. DAI is an early-stage open source project and contributions of any kind are welcome — code, bug reports, docs, and ideas.

## Before you start

Read [docs/SECURITY.md](docs/SECURITY.md). The rules there are non-negotiable. PRs that log IPs, transmit credentials, or store prompt content on-chain will not be merged, no matter how well-written they are.

---

## Running the project locally

### Prerequisites

| Tool | Version | Required for |
|---|---|---|
| Python | 3.10+ | Orchestrator + node client |
| Node.js | 22+ | Smart contracts |
| Ollama | any | Real inference (optional — mock mode works without it) |

### 1. Clone and install

```bash
git clone https://github.com/quacktheplanet/dai.git
cd dai
pip install -r requirements.txt
```

### 2. Start the orchestrator

```bash
python protocol/orchestrator.py
```

Runs on `http://localhost:8000`. Leave this terminal open.

### 3. Connect a node (no Ollama needed)

Open a second terminal:

```bash
# Mock mode — simulates inference, no model download required
python compute/node.py --models llama3

# Real inference — requires Ollama running with a model pulled
python compute/node.py --ollama --auto
```

### 4. Send a test request

Open a third terminal:

```bash
python test_request.py --message "Hello from DAI"

# Check connected nodes and earnings
python test_request.py --status
```

### 5. Run the test suite

```bash
# Python tests (80 tests — orchestrator, verification, ledger, Merkle, agent runner)
pip install -r requirements-dev.txt
python -m pytest tests/ -q

# Smart contract tests
cd chain
npm install
npm test
```

### 6. Smart contracts (optional)

```bash
cd chain
npx hardhat run scripts/deploy.js   # deploys to local Hardhat network
```

---

## How to contribute

1. **Open an issue first** for anything beyond a typo fix — describe what you want to change and why. This prevents wasted effort if the direction doesn't fit the roadmap.

2. **Fork and branch** off `main`. Name your branch something descriptive: `feat/relay-layer`, `fix/heartbeat-timeout`, etc.

3. **Keep PRs focused.** One logical change per PR. If you're fixing a bug and spotted a refactor opportunity, open two PRs.

4. **Tests.** Python changes should pass `python -m pytest tests/ -q`. Smart contract changes should pass `npm test` in `chain/`. If you're adding new behaviour, add a test for it.

5. **No new dependencies without discussion.** Open an issue before adding a package.

6. **Commit messages.** Plain English, present tense: `Add scored routing for GPU nodes`, not `Added routing stuff`.

---

## Project structure

```
dai/
├── protocol/
│   ├── orchestrator.py     # Network brain — WebSocket hub, task dispatch, REST API
│   ├── ledger.py           # In-memory token ledger
│   ├── chain_ledger.py     # On-chain extension — Merkle settlement, slashing, eligibility
│   ├── verification.py     # Pluggable Verifier seam (ContentVerifier + RedundantExecutionVerifier)
│   └── merkle.py           # Merkle tree for off-chain reward accrual and on-chain settlement
├── compute/
│   └── node.py             # Miner node client — connects to orchestrator, runs inference
├── application/
│   └── agent_runner.py     # Persistent agent on a token budget
├── shared/
│   └── schemas.py          # Pydantic models shared across layers
├── chain/
│   ├── contracts/          # Solidity smart contracts (DAIToken, PaymentContract, SlashingContract, MerkleDistributor)
│   ├── abis/               # Compiled ABIs for Python-side contract calls
│   ├── test/               # Contract test suite (Mocha + Hardhat v3)
│   └── scripts/            # Deployment scripts
├── tests/                  # Python test suite
├── docs/                   # Specs and design docs (SECURITY, VISION, VERIFICATION, ECONOMICS)
└── test_request.py         # CLI test tool
```

---

## Areas actively looking for help

- **Committee escalation** — when two nodes disagree on a result, a third arbitration path is needed before slashing can be automatic (see `docs/VERIFICATION_PROTOCOL.md`)
- **Reference inference stack** — pinned runtime + quantization params per model, so redundant verification has a well-defined comparison baseline
- **Front-end** — dashboard to visualize nodes, tasks, and earnings
- **Docs** — tutorials, model compatibility guides, architecture deep-dives
- **Ollama testing** — end-to-end tests with real inference (needs a local Ollama install)

---

## Questions?

Open a GitHub issue tagged `question`. We don't have a Discord yet — when there are enough people who want one, we'll set it up.
