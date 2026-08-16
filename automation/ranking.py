"""Deprecated alias for :mod:`automation.opportunity`.

The scoring system moved here to a cohort-aware "opportunity score" (see
``opportunity.py`` for the full rationale) — a candidate's age no longer
determines which formula applies via a single global normalisation, and
performance signals that used to be hard eligibility gates are inputs to this
score instead. Kept as a thin re-export so any existing ``from automation
import ranking`` / ``ranking.score_candidates(...)`` call site keeps working
unchanged.
"""
from __future__ import annotations

from .opportunity import (  # noqa: F401
    age_cohort, bayesian_engagement_rate, channel_outperformance, duration_fit,
    evergreen_strength, relevance_score, score_candidates,
)
