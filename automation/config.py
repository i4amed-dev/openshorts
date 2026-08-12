"""Autopilot configuration: defaults, validation and normalisation.

The whole Autopilot configuration is one JSON document persisted in the
``autopilot_settings`` row (see :mod:`automation.db`). It is edited from the
dashboard, so *every* field is attacker-controlled input: this module is the
single place that clamps it into something the orchestrator can trust.

Deliberately stdlib-only (plus ``zoneinfo``) so the package imports in CI
without the heavy video dependencies.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional

try:  # pragma: no cover - exercised implicitly on every supported platform
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore


class ConfigError(ValueError):
    """Raised when submitted settings cannot be normalised into valid config."""


# --- Source rights policy -----------------------------------------------------
# Manual mode keeps its per-request "I own this / have rights" checkbox. Autopilot
# has no human at the keyboard, so it carries a persistent policy instead and
# records the active policy on every autonomous processing decision. There is no
# path that fakes the manual checkbox.
POLICY_OWNED_OR_ALLOWLISTED = "OWNED_OR_ALLOWLISTED_CHANNELS"
POLICY_CREATIVE_COMMONS = "CREATIVE_COMMONS_ONLY"
POLICY_CC_OR_ALLOWLISTED = "CREATIVE_COMMONS_OR_ALLOWLISTED"
RIGHTS_POLICIES = (
    POLICY_CREATIVE_COMMONS,
    POLICY_OWNED_OR_ALLOWLISTED,
    POLICY_CC_OR_ALLOWLISTED,
)

PLATFORMS = ("tiktok", "instagram", "youtube")
DISCOVERY_STRATEGIES = ("most_popular", "niche_search")
CATCH_UP_POLICIES = ("next_slot", "skip", "immediate")
CLIP_SELECTION_MODES = ("top_n", "all")
DEFINITIONS = ("any", "sd", "hd")

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_YT_CHANNEL_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")

# Ranking weights. Every component is normalised to 0..1 within the candidate
# set before weighting, so no single raw count (a 40M-view video) can dominate.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "velocity": 0.30,      # views per hour since publication, rank-normalised
    "views": 0.10,         # absolute reach, log-compressed
    "engagement": 0.20,    # (likes + comments) / views
    "comments": 0.05,      # comment activity on its own (discussion = clippable)
    "recency": 0.15,       # newer inside the configured age window
    "chart_rank": 0.10,    # position in YouTube's mostPopular response
    "relevance": 0.10,     # keyword/topic match against configured niche
    "duration_fit": 0.05,  # sources that yield several good clips
}
DEFAULT_PENALTIES: Dict[str, float] = {
    "channel_repeat": 0.25,   # per prior source from the same channel (capped)
    "previously_seen": 0.10,  # discovered before but never processed
}

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "timezone": "UTC",
    "discovery": {
        "strategies": ["most_popular"],
        "region_code": "US",
        "relevance_language": "en",
        "category_ids": [],
        "topics": [],
        "max_candidates_per_run": 50,
        "channel_allowlist": [],
        "channel_denylist": [],
        "creative_commons_search_only": True,
    },
    "eligibility": {
        "min_duration_seconds": 180,
        "max_duration_seconds": 5400,
        "max_age_hours": 168,
        "min_views": 5000,
        "min_view_velocity_per_hour": 50.0,
        "min_engagement_rate": 0.005,
        "require_captions": False,
        "min_definition": "hd",
        "exclude_made_for_kids": True,
        "exclude_age_restricted": True,
        "channel_cooldown_hours": 168,
        "keywords_any": [],
        "keywords_none": [],
    },
    "rights": {
        "policy": POLICY_CREATIVE_COMMONS,
        "allowlisted_channel_ids": [],
    },
    "ranking": {
        "weights": dict(DEFAULT_WEIGHTS),
        "penalties": dict(DEFAULT_PENALTIES),
    },
    "schedule": {
        "days_of_week": [0, 1, 2, 3, 4, 5, 6],   # 0 = Monday (datetime.weekday())
        "discovery_times": ["03:00"],
        "publish_times": ["11:30", "16:30", "21:00"],
        "max_posts_per_day": 3,
        "max_sources_per_day": 1,
        "min_spacing_minutes": 120,
        "catch_up_policy": "next_slot",
        "horizon_days": 14,
    },
    "clips": {
        "max_clips_per_source": 3,
        "min_clip_seconds": 15.0,
        "max_clip_seconds": 60.0,
        "selection": "top_n",
    },
    "publishing": {
        "platforms": ["tiktok", "instagram", "youtube"],
        "upload_post_user": None,
    },
    "limits": {
        "max_process_attempts": 2,
        "max_publish_attempts": 3,
        "consecutive_failure_limit": 5,
        "youtube_daily_quota_units": 10000,
    },
    "processing": {
        "output_format": "vertical",
        "min_source_height": 720,
    },
}


# --- coercion helpers ---------------------------------------------------------

def _as_int(value: Any, *, default: int, lo: int, hi: int, field: str) -> int:
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{field} must be a whole number")
    return max(lo, min(hi, n))


def _as_float(value: Any, *, default: float, lo: float, hi: float, field: str) -> float:
    if value is None:
        return default
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{field} must be a number")
    if n != n:  # NaN
        raise ConfigError(f"{field} must be a number")
    return max(lo, min(hi, n))


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_str_list(value: Any, *, default: List[str], max_items: int = 100,
                 max_len: int = 200, lower: bool = False) -> List[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        value = [part for part in re.split(r"[,\n]", value)]
    if not isinstance(value, (list, tuple)):
        return list(default)
    out: List[str] = []
    for item in value:
        text = str(item).strip()[:max_len]
        if lower:
            text = text.lower()
        if text and text not in out:
            out.append(text)
        if len(out) >= max_items:
            break
    return out


def _as_times(value: Any, *, default: List[str], max_items: int, field: str) -> List[str]:
    raw = _as_str_list(value, default=default, max_items=max_items, max_len=5)
    out: List[str] = []
    for item in raw:
        if not _TIME_RE.match(item):
            raise ConfigError(f"{field} entries must look like HH:MM (got {item!r})")
        if item not in out:
            out.append(item)
    if not out:
        raise ConfigError(f"{field} needs at least one HH:MM entry")
    return sorted(out)


def validate_timezone(name: Any) -> str:
    """Return ``name`` if it is a loadable IANA zone, else raise ConfigError.

    Rejecting here (rather than at use) keeps a typo from silently shifting
    every future publishing slot by hours.
    """
    text = str(name or "").strip()
    if not text or len(text) > 64 or not re.match(r"^[A-Za-z0-9_+\-/]+$", text):
        raise ConfigError(f"Invalid timezone: {name!r}")
    if ZoneInfo is None:  # pragma: no cover
        return text
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise ConfigError(f"Unknown IANA timezone: {text!r}")
    return text


def _normalise_weights(raw: Any, defaults: Dict[str, float], field: str) -> Dict[str, float]:
    out = dict(defaults)
    if isinstance(raw, dict):
        for key in defaults:
            if key in raw:
                out[key] = _as_float(raw[key], default=defaults[key], lo=0.0, hi=10.0,
                                     field=f"{field}.{key}")
    return out


# --- public API ---------------------------------------------------------------

def normalise(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge ``raw`` over the defaults and clamp every field into range.

    Unknown keys are dropped rather than preserved: the stored document is the
    orchestrator's contract, and silently carrying junk forward makes a bad
    write survive every later edit.
    """
    raw = raw if isinstance(raw, dict) else {}
    cfg = copy.deepcopy(DEFAULTS)

    cfg["enabled"] = _as_bool(raw.get("enabled"), default=False)
    cfg["timezone"] = validate_timezone(raw.get("timezone") or DEFAULTS["timezone"])

    # --- discovery
    src = raw.get("discovery") if isinstance(raw.get("discovery"), dict) else {}
    d = cfg["discovery"]
    strategies = _as_str_list(src.get("strategies"), default=d["strategies"],
                              max_items=len(DISCOVERY_STRATEGIES), lower=True)
    d["strategies"] = [s for s in strategies if s in DISCOVERY_STRATEGIES] or ["most_popular"]
    region = str(src.get("region_code") or d["region_code"]).strip().upper()
    if not re.match(r"^[A-Z]{2}$", region):
        raise ConfigError("region_code must be a 2-letter ISO country code")
    d["region_code"] = region
    lang = str(src.get("relevance_language") or d["relevance_language"]).strip().lower()
    if not re.match(r"^[a-z]{2}(-[a-z0-9]{2,8})?$", lang):
        raise ConfigError("relevance_language must be a language code like 'en' or 'pt-br'")
    d["relevance_language"] = lang
    d["category_ids"] = [c for c in _as_str_list(src.get("category_ids"), default=[], max_items=20)
                         if c.isdigit()]
    d["topics"] = _as_str_list(src.get("topics"), default=[], max_items=25, max_len=80)
    d["max_candidates_per_run"] = _as_int(src.get("max_candidates_per_run"), default=50,
                                          lo=5, hi=200, field="discovery.max_candidates_per_run")
    d["channel_allowlist"] = _as_str_list(src.get("channel_allowlist"), default=[], max_items=200)
    d["channel_denylist"] = _as_str_list(src.get("channel_denylist"), default=[], max_items=200)
    d["creative_commons_search_only"] = _as_bool(src.get("creative_commons_search_only"),
                                                 default=True)
    if "niche_search" in d["strategies"] and not d["topics"]:
        raise ConfigError("The niche search strategy needs at least one topic/keyword")

    # --- eligibility
    src = raw.get("eligibility") if isinstance(raw.get("eligibility"), dict) else {}
    e = cfg["eligibility"]
    e["min_duration_seconds"] = _as_int(src.get("min_duration_seconds"), default=180,
                                        lo=30, hi=36000, field="eligibility.min_duration_seconds")
    e["max_duration_seconds"] = _as_int(src.get("max_duration_seconds"), default=5400,
                                        lo=60, hi=36000, field="eligibility.max_duration_seconds")
    if e["max_duration_seconds"] <= e["min_duration_seconds"]:
        raise ConfigError("eligibility.max_duration_seconds must exceed min_duration_seconds")
    e["max_age_hours"] = _as_int(src.get("max_age_hours"), default=168,
                                 lo=1, hi=24 * 365, field="eligibility.max_age_hours")
    e["min_views"] = _as_int(src.get("min_views"), default=5000,
                             lo=0, hi=1_000_000_000, field="eligibility.min_views")
    e["min_view_velocity_per_hour"] = _as_float(src.get("min_view_velocity_per_hour"),
                                                default=50.0, lo=0.0, hi=10_000_000.0,
                                                field="eligibility.min_view_velocity_per_hour")
    e["min_engagement_rate"] = _as_float(src.get("min_engagement_rate"), default=0.005,
                                         lo=0.0, hi=1.0,
                                         field="eligibility.min_engagement_rate")
    e["require_captions"] = _as_bool(src.get("require_captions"), default=False)
    definition = str(src.get("min_definition") or e["min_definition"]).strip().lower()
    e["min_definition"] = definition if definition in DEFINITIONS else "hd"
    e["exclude_made_for_kids"] = _as_bool(src.get("exclude_made_for_kids"), default=True)
    e["exclude_age_restricted"] = _as_bool(src.get("exclude_age_restricted"), default=True)
    e["channel_cooldown_hours"] = _as_int(src.get("channel_cooldown_hours"), default=168,
                                          lo=0, hi=24 * 365,
                                          field="eligibility.channel_cooldown_hours")
    e["keywords_any"] = _as_str_list(src.get("keywords_any"), default=[], max_items=50,
                                     max_len=80, lower=True)
    e["keywords_none"] = _as_str_list(src.get("keywords_none"), default=[], max_items=50,
                                      max_len=80, lower=True)

    # --- rights
    src = raw.get("rights") if isinstance(raw.get("rights"), dict) else {}
    policy = str(src.get("policy") or POLICY_CREATIVE_COMMONS).strip().upper()
    if policy not in RIGHTS_POLICIES:
        raise ConfigError(f"rights.policy must be one of {', '.join(RIGHTS_POLICIES)}")
    allow = _as_str_list(src.get("allowlisted_channel_ids"), default=[], max_items=200)
    bad = [c for c in allow if not _YT_CHANNEL_RE.match(c)]
    if bad:
        raise ConfigError(f"Not a YouTube channel id (expected UC…24 chars): {bad[0]}")
    cfg["rights"] = {"policy": policy, "allowlisted_channel_ids": allow}
    if policy in (POLICY_OWNED_OR_ALLOWLISTED, POLICY_CC_OR_ALLOWLISTED) and not allow:
        if policy == POLICY_OWNED_OR_ALLOWLISTED:
            raise ConfigError(
                "The owned/allowlisted rights policy needs at least one approved channel id")

    # --- ranking
    src = raw.get("ranking") if isinstance(raw.get("ranking"), dict) else {}
    cfg["ranking"] = {
        "weights": _normalise_weights(src.get("weights"), DEFAULT_WEIGHTS, "ranking.weights"),
        "penalties": _normalise_weights(src.get("penalties"), DEFAULT_PENALTIES,
                                        "ranking.penalties"),
    }

    # --- schedule
    src = raw.get("schedule") if isinstance(raw.get("schedule"), dict) else {}
    s = cfg["schedule"]
    days = src.get("days_of_week")
    if days is not None:
        parsed = []
        for item in (days if isinstance(days, (list, tuple)) else []):
            try:
                n = int(item)
            except (TypeError, ValueError):
                continue
            if 0 <= n <= 6 and n not in parsed:
                parsed.append(n)
        if not parsed:
            raise ConfigError("schedule.days_of_week needs at least one day (0=Mon … 6=Sun)")
        s["days_of_week"] = sorted(parsed)
    s["discovery_times"] = _as_times(src.get("discovery_times"), default=s["discovery_times"],
                                     max_items=6, field="schedule.discovery_times")
    s["publish_times"] = _as_times(src.get("publish_times"), default=s["publish_times"],
                                   max_items=12, field="schedule.publish_times")
    s["max_posts_per_day"] = _as_int(src.get("max_posts_per_day"), default=3, lo=1, hi=12,
                                     field="schedule.max_posts_per_day")
    s["max_sources_per_day"] = _as_int(src.get("max_sources_per_day"), default=1, lo=1, hi=10,
                                       field="schedule.max_sources_per_day")
    s["min_spacing_minutes"] = _as_int(src.get("min_spacing_minutes"), default=120, lo=0,
                                       hi=24 * 60, field="schedule.min_spacing_minutes")
    catch_up = str(src.get("catch_up_policy") or s["catch_up_policy"]).strip().lower()
    s["catch_up_policy"] = catch_up if catch_up in CATCH_UP_POLICIES else "next_slot"
    s["horizon_days"] = _as_int(src.get("horizon_days"), default=14, lo=1, hi=60,
                                field="schedule.horizon_days")

    # --- clips
    src = raw.get("clips") if isinstance(raw.get("clips"), dict) else {}
    c = cfg["clips"]
    c["max_clips_per_source"] = _as_int(src.get("max_clips_per_source"), default=3, lo=1, hi=15,
                                        field="clips.max_clips_per_source")
    c["min_clip_seconds"] = _as_float(src.get("min_clip_seconds"), default=15.0, lo=1.0, hi=600.0,
                                      field="clips.min_clip_seconds")
    c["max_clip_seconds"] = _as_float(src.get("max_clip_seconds"), default=60.0, lo=2.0, hi=600.0,
                                      field="clips.max_clip_seconds")
    if c["max_clip_seconds"] <= c["min_clip_seconds"]:
        raise ConfigError("clips.max_clip_seconds must exceed min_clip_seconds")
    selection = str(src.get("selection") or c["selection"]).strip().lower()
    c["selection"] = selection if selection in CLIP_SELECTION_MODES else "top_n"

    # --- publishing
    src = raw.get("publishing") if isinstance(raw.get("publishing"), dict) else {}
    platforms = _as_str_list(src.get("platforms"), default=list(PLATFORMS),
                             max_items=len(PLATFORMS), lower=True)
    platforms = [p for p in platforms if p in PLATFORMS]
    if not platforms:
        raise ConfigError(f"publishing.platforms must include at least one of {', '.join(PLATFORMS)}")
    user = src.get("upload_post_user")
    user = str(user).strip()[:128] if user not in (None, "") else None
    if user and not re.match(r"^[A-Za-z0-9._@-]+$", user):
        raise ConfigError("publishing.upload_post_user contains unsupported characters")
    cfg["publishing"] = {"platforms": platforms, "upload_post_user": user}

    # --- limits
    src = raw.get("limits") if isinstance(raw.get("limits"), dict) else {}
    lim = cfg["limits"]
    lim["max_process_attempts"] = _as_int(src.get("max_process_attempts"), default=2, lo=1, hi=5,
                                          field="limits.max_process_attempts")
    lim["max_publish_attempts"] = _as_int(src.get("max_publish_attempts"), default=3, lo=1, hi=10,
                                          field="limits.max_publish_attempts")
    lim["consecutive_failure_limit"] = _as_int(src.get("consecutive_failure_limit"), default=5,
                                               lo=1, hi=50,
                                               field="limits.consecutive_failure_limit")
    lim["youtube_daily_quota_units"] = _as_int(src.get("youtube_daily_quota_units"),
                                               default=10000, lo=100, hi=10_000_000,
                                               field="limits.youtube_daily_quota_units")

    # --- processing
    src = raw.get("processing") if isinstance(raw.get("processing"), dict) else {}
    p = cfg["processing"]
    fmt = str(src.get("output_format") or p["output_format"]).strip().lower()
    p["output_format"] = fmt if fmt in ("vertical", "horizontal", "square", "auto") else "vertical"
    p["min_source_height"] = _as_int(src.get("min_source_height"), default=720, lo=0, hi=4320,
                                     field="processing.min_source_height")

    return cfg


def default_settings() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULTS)
