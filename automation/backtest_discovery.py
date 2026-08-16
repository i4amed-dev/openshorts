"""Re-rank already-discovered candidates against the *current* config — no
network calls, no processing, no publishing.

Tuning `ranking.weights` or the rights policy by editing settings and waiting
for the next real discovery run is slow and burns YouTube quota on every
iteration. This reads whatever is already in the database, re-runs rights,
technical validity and opportunity scoring against today's config, and prints
what would happen — safe to run as often as you like while tuning.

Usage::

    python -m automation.backtest_discovery --limit 100 --show-top 20 --explain
    python -m automation.backtest_discovery --json > backtest.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

from . import eligibility, opportunity
from .config import normalise
from .db import AutopilotDB, DEFAULT_DB_PATH, parse_iso
from .discovery import _lane_of
from .models import DiscoveredSource
from .youtube_client import VideoRecord


def _source_to_record(source: DiscoveredSource) -> VideoRecord:
    """Reconstruct the VideoRecord a stored source was scored from.

    Lossy by design — the DB keeps what ranking/eligibility need, not the
    full API payload (no point storing fields nothing reads).
    """
    return VideoRecord(
        video_id=source.youtube_video_id,
        title=source.title,
        description=source.description,
        channel_id=source.channel_id,
        channel_title=source.channel_title,
        published_at=parse_iso(source.published_at),
        duration_seconds=source.duration_seconds,
        category_id=source.category_id,
        view_count=source.view_count,
        like_count=source.like_count,
        comment_count=source.comment_count,
        license=source.license,
        definition=source.definition,
        caption=source.caption_available,
        live_state=source.live_state,
        made_for_kids=source.made_for_kids,
        privacy_status=source.privacy_status,
        embeddable=source.embeddable,
        upload_status="processed",
        chart_rank=source.chart_rank,
        discovery_source=source.discovery_source,
    )


def run_backtest(db: AutopilotDB, *, limit: int = 200,
                 now: datetime | None = None) -> Dict[str, Any]:
    """Re-score up to ``limit`` most-recently-discovered sources.

    Returns the same shape whether called from the CLI or a test: ranked
    candidates (best first), a rejection-reason breakdown, and a lane
    distribution — the three things spec'd for the backtest command.
    """
    now = now or datetime.now(timezone.utc)
    config = normalise(db.load_settings())
    sources = db.list_sources(states=None, limit=limit, order="recent")
    records = [_source_to_record(s) for s in sources]
    known = {s.youtube_video_id for s in sources}
    channel_counts = {r.channel_id: db.channel_use_count(r.channel_id)
                      for r in records if r.channel_id}

    discovery_lanes = {r.video_id: _lane_of(r.discovery_source) for r in records}
    scored = opportunity.score_candidates(
        records, config, now=now, channel_use_counts=channel_counts, previously_seen=known,
        discovery_lanes=discovery_lanes)

    reasons: Dict[str, int] = {}
    lanes: Dict[str, int] = {}
    ranked: List[Dict[str, Any]] = []
    for record, score, breakdown in scored:
        rights_ok, rights_reason = eligibility.check_rights(record, config.get("rights") or {})
        technical_ok, technical_reason = eligibility.check_eligibility(record, config, now=now)
        ok = rights_ok and technical_ok
        reason = rights_reason if not rights_ok else technical_reason
        lane = breakdown.get("discovery_lane") or "unknown"
        lanes[lane] = lanes.get(lane, 0) + 1
        if not ok:
            reasons[reason or "unknown"] = reasons.get(reason or "unknown", 0) + 1
        ranked.append({
            "video_id": record.video_id, "title": record.title, "channel": record.channel_title,
            "score": score, "lane": lane, "age_cohort": breakdown.get("age_cohort"),
            "would_be_eligible": ok, "rejection_reason": reason,
            "breakdown": breakdown,
        })

    ranked.sort(key=lambda item: (-item["score"], item["video_id"]))
    return {
        "config_snapshot": {
            "rights_policy": (config.get("rights") or {}).get("policy"),
            "lanes": (config.get("discovery") or {}).get("lanes"),
            "selection": config.get("selection"),
        },
        "total_candidates": len(ranked),
        "would_be_eligible": sum(1 for item in ranked if item["would_be_eligible"]),
        "rejection_reasons": reasons,
        "lane_distribution": lanes,
        "ranked": ranked,
    }


def _print_text(result: Dict[str, Any], *, show_top: int, explain: bool) -> None:
    print(f"Candidates re-scored: {result['total_candidates']}")
    print(f"Would be eligible under current config: {result['would_be_eligible']}")
    print()
    print("Rejection reasons:")
    for reason, count in sorted(result["rejection_reasons"].items(), key=lambda kv: -kv[1]):
        print(f"  {reason:<40} {count}")
    print()
    print("Lane distribution:")
    for lane, count in sorted(result["lane_distribution"].items(), key=lambda kv: -kv[1]):
        print(f"  {lane:<24} {count}")
    print()
    print(f"Top {show_top} by opportunity score:")
    for item in result["ranked"][:show_top]:
        flag = "eligible" if item["would_be_eligible"] else f"blocked: {item['rejection_reason']}"
        print(f"  {item['score']:6.1f}  [{item['lane']:<18}] {item['title'][:60]!r} — {flag}")
        if explain:
            components = item["breakdown"].get("components", {})
            for key, value in components.items():
                print(f"           {key:<24} {value:.3f}")


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-rank already-discovered Autopilot candidates against the current "
                    "config. Read-only: no YouTube calls, no processing, no publishing.")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH,
                       help="Path to the Autopilot SQLite database.")
    parser.add_argument("--limit", type=int, default=200,
                       help="How many recently-discovered rows to re-score.")
    parser.add_argument("--show-top", type=int, default=20,
                       help="How many top-ranked candidates to print.")
    parser.add_argument("--explain", action="store_true",
                       help="Print each shown candidate's full score breakdown.")
    parser.add_argument("--json", action="store_true",
                       help="Print machine-readable JSON instead of a text report.")
    args = parser.parse_args(argv)

    db = AutopilotDB(args.db_path).connect()
    try:
        result = run_backtest(db, limit=args.limit)
    finally:
        db.close()

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_text(result, show_top=args.show_top, explain=args.explain)
    return 0


if __name__ == "__main__":
    sys.exit(main())
