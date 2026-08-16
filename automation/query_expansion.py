"""Deterministic query expansion for topic-based discovery lanes.

A configured topic like ``"AI"`` is one query; a source video hiding under
"AI tutorial" or "AI mistakes" never surfaces if that is the only string ever
sent to ``search.list``. Expanding each topic into a bounded set of intent
variants finds more of the niche without an unbounded, quota-exploding fan-out
— and without an LLM call: the variant list is a fixed, deterministic
template set, rotated across runs so the *same* topic explores a different
angle each time rather than exhausting quota by trying every angle at once.

No language model is involved here on purpose. A bounded deterministic list
is reproducible, free, and instant; an LLM-assisted version could be added
later as a cached, optional refinement (see the module docstring in
automation/opportunity.py for the same reasoning applied to scoring) but
would need its own port, since automation/ stays stdlib-only.
"""
from __future__ import annotations

import hashlib
from typing import List

# Deliberately generic intent templates — they work for almost any topic
# string without producing nonsense ("AI vs", "AI explained", "best AI").
_TEMPLATES: List[str] = [
    "{topic}",
    "{topic} tools",
    "{topic} tutorial",
    "{topic} explained",
    "{topic} mistakes",
    "{topic} news",
    "{topic} experiment",
    "{topic} workflow",
    "{topic} productivity",
    "{topic} future",
    "{topic} vs",
    "best {topic}",
    "{topic} changed",
    "{topic} story",
]


def _stable_index(topic: str, run_index: int, modulus: int) -> int:
    """Deterministic, evenly-distributed rotation offset for one topic/run pair."""
    digest = hashlib.sha256(f"{topic}:{run_index}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % modulus


def expand_topic(topic: str, *, variants_per_run: int = 2, run_index: int = 0) -> List[str]:
    """Return up to ``variants_per_run`` query strings for ``topic`` this run.

    The literal topic itself is always included first (never expanded away),
    then a rotating subset of the remaining templates fills the rest — so
    over several runs the same topic is searched from different angles
    without ever exceeding the per-run budget in one call.
    """
    topic = (topic or "").strip()
    if not topic:
        return []
    variants_per_run = max(1, int(variants_per_run))
    if variants_per_run == 1:
        return [topic]

    rest = _TEMPLATES[1:]
    offset = _stable_index(topic, run_index, len(rest))
    rotated = rest[offset:] + rest[:offset]

    out = [topic]
    seen = {topic.lower()}
    for template in rotated:
        if len(out) >= variants_per_run:
            break
        candidate = template.format(topic=topic).strip()
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out
