"""Recall fusion weights -- the single authority every ranker reads.

Why this is its own module (OPT-02-D): `recall_profile` needs the default weight
table, and `memory_lifecycle` (the ranker) imports `recall_profile`. Keeping the
table inside `memory_lifecycle` therefore forced `recall_profile.weights_for()`
to do a *lazy* import back into `memory_lifecycle` just to read a constant --
a circular edge that only worked because one side deferred it to call time.
Downgrading it to a value module (no imports, no side effects) removes the edge
instead of hiding it. `memory_lifecycle.RECALL_WEIGHTS` remains importable as a
re-export, so callers and tests that read it keep working unchanged.

Why an atomic table and not computed weights: recall must stay reproducible and
auditable. `RECALL_WEIGHTS` is a stable rule, not an adaptive estimate -- no
feedback loop rewrites it at runtime.

2026-11-16 (S5-E): relevance share raised 0.55 -> 0.8652, measured on the
41-answer set through the real `recall_profile` clamp path:
  R@1 0.6585 -> 0.7317 (+0.0732), MRR@5 0.7724 -> 0.8211 (+0.0487),
  nDCG@5 0.8118 -> 0.8484 (+0.0366), R@3/R@5 unchanged (0.9268 both).
The gain does not come from "adding relevance" but from *not double counting*
the governance columns: `importance_score` already folds frequency (0.25) and
confidence (0.20) into itself, so keeping both the aggregate and its inputs at
full weight fed the same signal in twice. Every component keeps a non-zero
weight -- relevance dominates, the priors only break ties. See
S5E_IMPLEMENTATION_REPORT.md for the full A/T1..T4 ablation table.
"""
from __future__ import annotations

RECALL_WEIGHTS = {
    "semantic": 0.27,
    "keyword": 0.60,
    "importance": 0.03,
    "confidence": 0.03,
    "freshness": 0.03,
    "frequency": 0.02,
    "activation": 0.02,
}
