"""Channel-relative performance: is this candidate big for its own channel?

A 2M-view video from a channel that normally gets 3M views is unremarkable.
A 700K-view video from a channel that normally gets 40K views is extremely
interesting — the video is doing 17x what this channel usually does, which
raw view count alone can never show. That is what ``channel_outperformance``
in automation/opportunity.py needs, and this module is where the baseline it
compares against comes from.

The baseline is deliberately cheap: ``channel.view_count / channel.video_count``
from one batched ``channels.list`` call, not a sampled average of the
channel's recent uploads (which would need its own quota-hungry
``search.list``/``playlistItems.list`` pass per channel). That is a real
limitation — a channel whose output quality changed sharply over time gets a
less accurate baseline — but it is quota-free beyond the one batched call,
which is what makes it affordable to compute for every candidate's channel on
every discovery run. See the final engineering report for the tradeoff.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Optional

from .db import AutopilotDB
from .youtube_client import YouTubeClient

CACHE_TTL_HOURS = 24 * 7  # a week: subscriber/view counts drift slowly


async def get_channel_baselines(db: AutopilotDB, client: YouTubeClient,
                                channel_ids: Iterable[str], *,
                                now: Optional[datetime] = None) -> Dict[str, float]:
    """views-per-video baseline for each channel id, fetching only what is stale.

    Returns a plain ``{channel_id: baseline}`` map with only channels that had
    usable statistics — a missing entry means "no data", which
    ``opportunity.channel_outperformance`` already treats as neutral rather
    than as a low score.
    """
    now = now or datetime.now(timezone.utc)
    ids = [c for c in dict.fromkeys(channel_ids) if c]
    if not ids:
        return {}

    cached = db.get_channel_stats_bulk(ids)
    stale_cutoff = now - timedelta(hours=CACHE_TTL_HOURS)
    to_fetch = []
    for channel_id in ids:
        row = cached.get(channel_id)
        if row is None:
            to_fetch.append(channel_id)
            continue
        from .db import parse_iso
        fetched_at = parse_iso(row.get("fetched_at"))
        if fetched_at is None or fetched_at < stale_cutoff:
            to_fetch.append(channel_id)

    if to_fetch and client.configured:
        records = await client.channels(to_fetch)
        for record in records:
            db.save_channel_stats(
                record.channel_id, subscriber_count=record.subscriber_count,
                view_count=record.view_count, video_count=record.video_count)
        cached.update(db.get_channel_stats_bulk(to_fetch))

    baselines: Dict[str, float] = {}
    for channel_id in ids:
        row = cached.get(channel_id)
        if not row:
            continue
        views = row.get("view_count")
        videos = row.get("video_count")
        if views and videos and videos > 0:
            baselines[channel_id] = float(views) / float(videos)
    return baselines
