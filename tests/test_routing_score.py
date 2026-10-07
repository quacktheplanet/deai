"""
Routing must not give a score advantage for self-reported GPU/VRAM
(build-now #3). Model match and the idle round-robin tiebreaker stay.
"""

from types import SimpleNamespace

import orchestrator


def _node(models, gpu=False, vram_gb=None, last_task_time=0.0, project=None, warm=True):
    return SimpleNamespace(
        info=SimpleNamespace(models=models, gpu=gpu, vram_gb=vram_gb, project=project, warm=warm),
        last_task_time=last_task_time,
    )


def test_gpu_claim_gives_no_score_advantage():
    cpu = _node(["llama3"], gpu=False, vram_gb=None, last_task_time=0.0)
    gpu = _node(["llama3"], gpu=True, vram_gb=80, last_task_time=0.0)
    assert orchestrator.score_node(cpu, "llama3") == orchestrator.score_node(gpu, "llama3")


def test_exact_model_still_beats_any():
    exact = _node(["llama3"], last_task_time=0.0)
    generic = _node(["any"], last_task_time=0.0)
    assert orchestrator.score_node(exact, "llama3") > orchestrator.score_node(generic, "llama3")


def test_unrunnable_model_rejected():
    n = _node(["mistral"], gpu=True, vram_gb=80)
    assert orchestrator.score_node(n, "llama3") == -1


def test_vram_does_not_change_score():
    lo = _node(["any"], gpu=True, vram_gb=4, last_task_time=0.0)
    hi = _node(["any"], gpu=True, vram_gb=80, last_task_time=0.0)
    assert orchestrator.score_node(lo, "llama3") == orchestrator.score_node(hi, "llama3")


# ── Dedicated mining ──────────────────────────────────────────────────────────

def test_dedicated_node_rejected_for_wrong_project():
    n = _node(["any"], project="acme")
    assert orchestrator.score_node(n, "any", project="other") == -1


def test_dedicated_node_rejected_for_untagged_task():
    n = _node(["any"], project="acme")
    assert orchestrator.score_node(n, "any", project=None) == -1


def test_dedicated_node_accepted_for_matching_project():
    n = _node(["any"], project="acme")
    assert orchestrator.score_node(n, "any", project="acme") >= 0


def test_general_node_accepts_any_project():
    n = _node(["any"], project=None)
    assert orchestrator.score_node(n, "any", project="acme") >= 0
    assert orchestrator.score_node(n, "any", project=None) >= 0


def test_dedicated_node_scores_higher_than_general_for_its_project():
    dedicated = _node(["any"], project="acme", last_task_time=0.0)
    general   = _node(["any"], project=None,   last_task_time=0.0)
    assert orchestrator.score_node(dedicated, "any", project="acme") > \
           orchestrator.score_node(general,   "any", project="acme")


# ── Sleeping nodes ────────────────────────────────────────────────────────────

def test_warm_node_preferred_over_sleeping_one():
    warm = _node(["llama3"], last_task_time=0.0)
    asleep = _node(["llama3"], last_task_time=0.0, warm=False)
    assert orchestrator.score_node(warm, "llama3") > orchestrator.score_node(asleep, "llama3")


def test_sleeping_node_still_eligible():
    assert orchestrator.score_node(_node(["llama3"], warm=False), "llama3") >= 0


def test_exact_model_match_outweighs_warmth():
    """A sleeping node with the exact model still beats a warm 'any' node."""
    exact_asleep = _node(["llama3"], warm=False)
    generic_warm = _node(["any"])
    assert orchestrator.score_node(exact_asleep, "llama3") > orchestrator.score_node(generic_warm, "llama3")
