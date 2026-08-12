"""HTTP surface for the Autopilot dashboard.

Read endpoints are cheap SQLite reads; write endpoints are operator actions.
Nothing here returns a credential: :meth:`AutopilotService.status` reports only
whether each one is *present*.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .config import ConfigError
from .service import get_service

router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])


class SettingsPatch(BaseModel):
    """A partial settings document. Validation lives in automation.config."""
    model_config = {"extra": "allow"}


def _service():
    return get_service()


@router.get("/status")
async def autopilot_status():
    return _service().status()


@router.get("/settings")
async def get_settings():
    return _service().get_settings()


@router.put("/settings")
async def put_settings(request: Request):
    try:
        patch = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    try:
        return _service().update_settings(patch)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/enable")
async def enable():
    try:
        config = _service().enable()
    except ConfigError as exc:
        # Enabling with an incomplete configuration (no topics for niche search,
        # no allowlisted channels for the owned-channels policy) must fail here
        # rather than at 03:00 with nobody watching.
        raise HTTPException(status_code=400, detail=str(exc))
    return {"enabled": True, "settings": config}


@router.post("/disable")
async def disable():
    _service().disable()
    return {"enabled": False}


@router.post("/pause")
async def pause():
    _service().pause()
    return {"ok": True}


@router.post("/resume")
async def resume():
    _service().resume()
    return {"ok": True}


@router.post("/emergency-stop")
async def emergency_stop():
    return _service().emergency_stop()


@router.post("/discover")
async def discover_now():
    return await _service().run_discovery_now()


@router.post("/process-next")
async def process_next():
    return await _service().process_next_now()


@router.post("/sources/{source_id}/skip")
async def skip_source(source_id: int):
    if not _service().skip_source(source_id):
        raise HTTPException(status_code=409, detail="This source can no longer be skipped")
    return {"ok": True}


@router.post("/sources/{source_id}/retry")
async def retry_source(source_id: int):
    if not _service().retry_source(source_id):
        raise HTTPException(status_code=409, detail="This source cannot be retried")
    return {"ok": True}


@router.get("/sources/{source_id}")
async def get_source(source_id: int):
    service = _service()
    source = service.db.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    from .service import _publish_view, _source_view
    config = service.get_settings()
    return {
        "source": _source_view(source),
        "clips": [
            {"id": c.id, "clip_index": c.clip_index, "title": c.title,
             "filename": c.filename, "state": c.state, "skip_reason": c.skip_reason,
             "duration": round(c.duration, 2)}
            for c in service.db.list_clips(source_id=source_id)
        ],
        "processing_attempts": service.db.list_processing_attempts(source_id),
        "publish_attempts": [_publish_view(a, config) for a in
                             service.db.list_publish_attempts(source_id=source_id, limit=50)],
        "events": service.db.recent_events(limit=60, source_id=source_id),
    }


@router.post("/publishes/{attempt_id}/retry")
async def retry_publish(attempt_id: int):
    if not _service().retry_publish(attempt_id):
        raise HTTPException(status_code=409,
                            detail="Only a failed attempt can be retried")
    return {"ok": True}


@router.post("/publishes/{attempt_id}/force-retry")
async def force_retry_publish(attempt_id: int):
    """Re-send an UNCERTAIN attempt. The caller has confirmed it did not post."""
    if not _service().force_retry_uncertain(attempt_id):
        raise HTTPException(status_code=409, detail="This attempt is not uncertain")
    return {"ok": True}


@router.post("/publishes/{attempt_id}/resolve")
async def resolve_publish(attempt_id: int):
    """Close out an UNCERTAIN attempt the operator verified on the calendar."""
    if not _service().resolve_uncertain(attempt_id):
        raise HTTPException(status_code=409, detail="This attempt is not uncertain")
    return {"ok": True}


@router.get("/events")
async def events(limit: int = 100, level: Optional[str] = None):
    return {"events": _service().db.recent_events(limit=limit, level=level)}
