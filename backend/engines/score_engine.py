"""Phase C — score engine: transparent summation of RuleResults.

THE SCORE IS NOT A PROBABILITY. It measures configured rule confirmation only.
It must never be displayed as %, accuracy, chance of profit or likelihood.

NO SCORE NORMALIZATION (V1): an unavailable rule's points are never
redistributed — 70/80 achievable stays 70/100, not 87.5/100.
"""

from __future__ import annotations

from typing import Sequence

from models.signal_models import RuleResult


def score_rules(rules: Sequence[RuleResult]) -> tuple[int, int]:
    """Return (awarded, max). Max is the FULL configured weight sum (100),
    regardless of how many rules were available — availability is tracked on
    the RuleResults themselves, never by rescaling the denominator."""
    awarded = sum(r.score_awarded for r in rules)
    max_score = sum(r.max_score for r in rules)
    return awarded, max_score


def explain_side(side: str, rules: Sequence[RuleResult], score: int, max_score: int, minimum_score: int) -> list[str]:
    """Human-readable explanation lines for one side (score, passed, failed,
    unavailable) — used in WAIT reasons and signal explanations."""
    lines = [f"{side} {score}/{max_score} vs minimum {minimum_score}"]
    passed = [r for r in rules if r.available and r.passed]
    failed = [r for r in rules if r.available and not r.passed]
    unavailable = [r for r in rules if not r.available]
    if passed:
        lines.append(f"{side} passed: " + "; ".join(f"{r.rule_id} ({r.score_awarded}/{r.max_score})" for r in passed))
    if failed:
        lines.append(f"{side} failed: " + "; ".join(f"{r.rule_id} — {r.reason}" for r in failed))
    if unavailable:
        lines.append(f"{side} unavailable: " + "; ".join(f"{r.rule_id} — {r.reason}" for r in unavailable))
    return lines
