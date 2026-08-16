"""/settings — guided editing, never raw JSON. Every mutation funnels through
`service.update_settings(patch)`, which already validates via `automation.config`
— this module never re-implements that validation, it just surfaces
`ConfigError` messages and lets the operator try again.
"""
from __future__ import annotations

from typing import Any, Dict

from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from .. import auth, callbacks, errors, navigation, render
from ..callbacks import Callback, router

PROMPT_KEY = "settings_prompt"


async def _settings() -> Dict[str, Any]:
    from automation.service import get_service
    return get_service().get_settings()


def _msg_settings(settings: Dict[str, Any]) -> str:
    disc = settings.get("discovery") or {}
    elig = settings.get("eligibility") or {}
    sched = settings.get("schedule") or {}
    rights = settings.get("rights") or {}
    pubs = settings.get("publishing") or {}
    topics = disc.get("topics") or []

    lines = [
        "⚙️ " + render.bold("Settings"), "",
        render.bold("Discovery:"),
        f"  Strategies: {render.esc(', '.join(disc.get('strategies', [])))}",
        f"  Region: {render.esc(disc.get('region_code', 'US'))}",
        f"  Topics ({len(topics)}): {render.esc(', '.join(topics[:4]))}"
        f"{'…' if len(topics) > 4 else ''}",
        "",
        render.bold("Eligibility:"),
        f"  Min views: {render.count(elig.get('min_views', 0))}",
        f"  Min velocity: {elig.get('min_view_velocity_per_hour', 0)}/hr",
        f"  Duration: {render.duration(elig.get('min_duration_seconds'))} – "
        f"{render.duration(elig.get('max_duration_seconds'))}",
        f"  Max age: {elig.get('max_age_hours', 168)}h",
        "",
        render.bold("Schedule:"),
        f"  Publish times: {render.esc(', '.join(sched.get('publish_times', [])))}",
        f"  Discovery times: {render.esc(', '.join(sched.get('discovery_times', [])))}",
        f"  Max posts/day: {sched.get('max_posts_per_day')}",
        f"  Max sources/day: {sched.get('max_sources_per_day')}",
        f"  Timezone: {render.esc(settings.get('timezone', 'UTC'))}",
        "",
        f"{render.bold('Rights policy:')} {render.esc(rights.get('policy', '—').replace('_', ' ').title())}",
        f"{render.bold('Platforms:')} {render.esc(', '.join(pubs.get('platforms', [])))}",
    ]
    return "\n".join(lines)


def _section_detail(settings: Dict[str, Any], section: str) -> str:
    disc = settings.get("discovery") or {}
    elig = settings.get("eligibility") or {}
    sched = settings.get("schedule") or {}
    pubs = settings.get("publishing") or {}

    if section == "discovery":
        topics = disc.get("topics") or []
        return (
            "🔍 " + render.bold("Discovery") + "\n\n"
            f"Strategies: {render.esc(', '.join(disc.get('strategies', [])))}\n"
            f"Region: {render.esc(disc.get('region_code', 'US'))}\n"
            f"Language: {render.esc(disc.get('relevance_language', 'en'))}\n"
            f"Max candidates/run: {disc.get('max_candidates_per_run', 50)}\n\n"
            f"{render.bold(f'Topics ({len(topics)}):')}\n" +
            "\n".join(f"• {render.esc(t)}" for t in topics)
        )
    if section == "eligibility":
        return (
            "📊 " + render.bold("Eligibility") + "\n\n"
            f"Min views: {render.count(elig.get('min_views'))}\n"
            f"Min velocity: {elig.get('min_view_velocity_per_hour')}/hr\n"
            f"Min duration: {render.duration(elig.get('min_duration_seconds'))}\n"
            f"Max duration: {render.duration(elig.get('max_duration_seconds'))}\n"
            f"Max age: {elig.get('max_age_hours')}h\n"
            f"Min engagement: {elig.get('min_engagement_rate')}\n"
            f"Min definition: {render.esc(elig.get('min_definition', 'any'))}\n"
            f"Require captions: {'yes' if elig.get('require_captions') else 'no'}\n"
            f"Exclude kids: {'yes' if elig.get('exclude_made_for_kids', True) else 'no'}"
        )
    if section == "schedule":
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        days = sched.get("days_of_week", list(range(7)))
        active = ", ".join(day_names[d] for d in days)
        return (
            "🕐 " + render.bold("Schedule") + "\n\n"
            f"Publish times: {render.esc(', '.join(sched.get('publish_times', [])))}\n"
            f"Discovery times: {render.esc(', '.join(sched.get('discovery_times', [])))}\n"
            f"Days: {render.esc(active)}\n"
            f"Max posts/day: {sched.get('max_posts_per_day')}\n"
            f"Max sources/day: {sched.get('max_sources_per_day')}\n"
            f"Min spacing: {sched.get('min_spacing_minutes')} min\n"
            f"Catch-up policy: {render.esc(sched.get('catch_up_policy', 'next_slot'))}"
        )
    if section == "platforms":
        return (
            "📤 " + render.bold("Platforms") + "\n\n"
            f"Active: {render.esc(', '.join(pubs.get('platforms', [])) or 'none')}\n\n"
            + render.italic("Guided editing lands in a later update.")
        )
    return "⚙️ " + render.italic("Unknown section.")


def _kb_settings():
    return navigation.kb(
        [InlineKeyboardButton("🔍 Discovery", callback_data=callbacks.build("settings", "section", "discovery")),
         InlineKeyboardButton("📊 Eligibility", callback_data=callbacks.build("settings", "section", "eligibility"))],
        [InlineKeyboardButton("🕐 Schedule", callback_data=callbacks.build("settings", "section", "schedule")),
         InlineKeyboardButton("📤 Platforms", callback_data=callbacks.build("settings", "section", "platforms"))],
        [InlineKeyboardButton("🛡 Rights", callback_data=callbacks.build("settings", "r_edit"))],
        navigation.nav_row(refresh=callbacks.build("settings", "show"), home=True),
    )


async def _service():
    from automation.service import get_service
    return get_service()


async def _prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str,
                   question: str) -> None:
    context.user_data[PROMPT_KEY] = {"kind": kind}
    kb = navigation.kb([InlineKeyboardButton(
        "❌ Cancel", callback_data=callbacks.build("settings", "prompt_cancel"))])
    await errors.deliver(update, context, question, kb)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Called from the global text handler. Returns True if it consumed the
    message (a settings prompt was pending), False otherwise."""
    state = context.user_data.get(PROMPT_KEY)
    if not state:
        return False
    if not await auth.guard(update, auth.Role.ADMIN, action="settings:prompt"):
        context.user_data.pop(PROMPT_KEY, None)
        return True

    context.user_data.pop(PROMPT_KEY, None)
    text = (update.message.text or "").strip()
    service = await _service()
    from automation.config import ConfigError

    try:
        if state["kind"] == "topic_add":
            topics = list((service.get_settings().get("discovery") or {}).get("topics") or [])
            if text and text not in topics:
                topics.append(text)
            service.update_settings({"discovery": {"topics": topics}})
        elif state["kind"] == "region":
            service.update_settings({"discovery": {"region_code": text.upper()[:2]}})
        elif state["kind"] == "allow_add":
            ids = list((service.get_settings().get("rights") or {}).get("allowlisted_channel_ids") or [])
            if text and text not in ids:
                ids.append(text)
            service.update_settings({"rights": {"allowlisted_channel_ids": ids}})
        elif state["kind"] == "time_add":
            times = list((service.get_settings().get("schedule") or {}).get("publish_times") or [])
            if text and text not in times:
                times.append(text)
            service.update_settings({"schedule": {"publish_times": sorted(times)}})
        elif state["kind"] == "max_posts":
            service.update_settings({"schedule": {"max_posts_per_day": int(text)}})
        elif state["kind"] == "max_sources":
            service.update_settings({"schedule": {"max_sources_per_day": int(text)}})
        await update.message.reply_text("✅ Updated.")
    except ConfigError as exc:
        await update.message.reply_text(f"❌ {exc}")
    except ValueError:
        await update.message.reply_text("❌ That doesn't look like a valid number.")
    return True


async def prompt_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(PROMPT_KEY, None)
    if update.callback_query is not None:
        await update.callback_query.answer("Cancelled.")
    await show(update, context)


# --- discovery: topics -------------------------------------------------------

async def discovery_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="settings:discovery_edit"):
        return
    service = await _service()
    topics = list((service.get_settings().get("discovery") or {}).get("topics") or [])
    text = "🔍 " + render.bold("Discovery — Topics") + "\n\n" + (
        "\n".join(f"• {render.esc(t)}" for t in topics) if topics else render.italic("No topics set."))
    rows = [[InlineKeyboardButton(f"🗑 {t}", callback_data=callbacks.build("settings", "d_topic_rm", i))]
            for i, t in enumerate(topics)]
    rows.append([InlineKeyboardButton("➕ Add", callback_data=callbacks.build("settings", "d_topic_add")),
                InlineKeyboardButton("✏️ Region", callback_data=callbacks.build("settings", "d_region"))])
    rows.append(navigation.nav_row(refresh=callbacks.build("settings", "d_edit"),
                                    back=callbacks.build("settings", "show")))
    await errors.deliver(update, context, text, navigation.kb(*rows))


async def topic_remove(update: Update, context: ContextTypes.DEFAULT_TYPE, index: int) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="settings:topic_remove"):
        return
    service = await _service()
    topics = list((service.get_settings().get("discovery") or {}).get("topics") or [])
    if 0 <= index < len(topics):
        topics.pop(index)
        service.update_settings({"discovery": {"topics": topics}})
    if update.callback_query is not None:
        await update.callback_query.answer("Removed.")
    await discovery_edit(update, context)


# --- rights -------------------------------------------------------------------

_POLICY_LABELS = {
    "CREATIVE_COMMONS_ONLY": "Creative Commons only",
    "OWNED_OR_ALLOWLISTED_CHANNELS": "Owned / approved channels",
    "CREATIVE_COMMONS_OR_ALLOWLISTED": "Creative Commons OR approved channels",
}


async def rights_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="settings:rights_edit"):
        return
    service = await _service()
    rights = service.get_settings().get("rights") or {}
    current = rights.get("policy")
    allow = list(rights.get("allowlisted_channel_ids") or [])
    text = (
        "🛡 " + render.bold("Rights Policy") + "\n\n"
        f"Current: {render.esc(_POLICY_LABELS.get(current, current))}\n\n"
        + render.italic("Changing this policy affects every future discovery pass — it is "
                        "never weakened silently.") + "\n\n"
        + render.bold(f"Approved channels ({len(allow)}):") + "\n"
        + ("\n".join(f"• {render.esc(c)}" for c in allow) if allow else render.italic("none"))
    )
    rows = [[InlineKeyboardButton(("✅ " if p == current else "") + label,
                                  callback_data=callbacks.build("settings", "r_policy", p))]
            for p, label in _POLICY_LABELS.items()]
    rows.append([InlineKeyboardButton("➕ Add channel", callback_data=callbacks.build("settings", "r_allow_add"))])
    rows += [[InlineKeyboardButton(f"🗑 {c}", callback_data=callbacks.build("settings", "r_allow_rm", i))]
            for i, c in enumerate(allow)]
    rows.append(navigation.nav_row(refresh=callbacks.build("settings", "r_edit"),
                                    back=callbacks.build("settings", "show")))
    await errors.deliver(update, context, text, navigation.kb(*rows))


async def rights_policy_confirm_screen(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                        policy: str) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="settings:rights_policy"):
        return
    label = _POLICY_LABELS.get(policy, policy)
    text = (
        "⚠️ " + render.bold("Confirm rights policy change") + "\n\n"
        f"Switch to: {render.esc(label)}\n\n"
        + render.italic("This changes what Autopilot is allowed to select going forward.")
    )
    kb = navigation.kb(
        [InlineKeyboardButton(f"✅ Yes, use “{label}”",
                              callback_data=callbacks.build("settings", "r_policy_go", policy))],
        [InlineKeyboardButton("❌ Cancel", callback_data=callbacks.build("settings", "r_edit"))])
    await errors.deliver(update, context, text, kb)


async def rights_policy_apply(update: Update, context: ContextTypes.DEFAULT_TYPE, policy: str) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="settings:rights_policy_go"):
        return
    service = await _service()
    from automation.config import ConfigError
    try:
        service.update_settings({"rights": {"policy": policy}})
        if update.callback_query is not None:
            await update.callback_query.answer("Rights policy updated.")
    except ConfigError as exc:
        if update.callback_query is not None:
            await update.callback_query.answer(str(exc), show_alert=True)
    await rights_edit(update, context)


async def allow_remove(update: Update, context: ContextTypes.DEFAULT_TYPE, index: int) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="settings:allow_remove"):
        return
    service = await _service()
    ids = list((service.get_settings().get("rights") or {}).get("allowlisted_channel_ids") or [])
    if 0 <= index < len(ids):
        ids.pop(index)
        try:
            service.update_settings({"rights": {"allowlisted_channel_ids": ids}})
        except Exception:
            pass
    if update.callback_query is not None:
        await update.callback_query.answer("Removed.")
    await rights_edit(update, context)


# --- schedule -------------------------------------------------------------------

async def schedule_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="settings:schedule_edit"):
        return
    service = await _service()
    sched = service.get_settings().get("schedule") or {}
    times = list(sched.get("publish_times") or [])
    text = (
        "🕐 " + render.bold("Publishing slots") + "\n\n"
        + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(times)) + "\n\n"
        f"Max posts/day: {sched.get('max_posts_per_day')}\n"
        f"Max sources/day: {sched.get('max_sources_per_day')}"
    )
    rows = [[InlineKeyboardButton(f"🗑 {t}", callback_data=callbacks.build("settings", "s_time_rm", i))]
            for i, t in enumerate(times)]
    rows.append([InlineKeyboardButton("➕ Add slot", callback_data=callbacks.build("settings", "s_time_add"))])
    rows.append([InlineKeyboardButton("✏️ Max posts/day", callback_data=callbacks.build("settings", "s_maxposts")),
                InlineKeyboardButton("✏️ Max sources/day", callback_data=callbacks.build("settings", "s_maxsources"))])
    rows.append(navigation.nav_row(refresh=callbacks.build("settings", "s_edit"),
                                    back=callbacks.build("settings", "show")))
    await errors.deliver(update, context, text, navigation.kb(*rows))


async def time_remove(update: Update, context: ContextTypes.DEFAULT_TYPE, index: int) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="settings:time_remove"):
        return
    service = await _service()
    from automation.config import ConfigError
    times = list((service.get_settings().get("schedule") or {}).get("publish_times") or [])
    if 0 <= index < len(times):
        times.pop(index)
        try:
            service.update_settings({"schedule": {"publish_times": times}})
            if update.callback_query is not None:
                await update.callback_query.answer("Removed.")
        except ConfigError as exc:
            if update.callback_query is not None:
                await update.callback_query.answer(str(exc), show_alert=True)
    await schedule_edit(update, context)


# --- publishing: platforms --------------------------------------------------------

async def platforms_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="settings:platforms_edit"):
        return
    from automation.config import PLATFORMS
    service = await _service()
    active = set((service.get_settings().get("publishing") or {}).get("platforms") or [])
    text = "📤 " + render.bold("Platforms") + "\n\n" + render.italic(
        "At least one platform must stay selected.")
    rows = [[InlineKeyboardButton(f"{'☑' if p in active else '☐'} {p}",
                                  callback_data=callbacks.build("settings", "p_toggle", p))]
            for p in PLATFORMS]
    rows.append(navigation.nav_row(refresh=callbacks.build("settings", "p_edit"),
                                    back=callbacks.build("settings", "show")))
    await errors.deliver(update, context, text, navigation.kb(*rows))


async def platform_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, platform: str) -> None:
    if not await auth.guard(update, auth.Role.ADMIN, action="settings:platform_toggle"):
        return
    service = await _service()
    from automation.config import ConfigError
    active = set((service.get_settings().get("publishing") or {}).get("platforms") or [])
    if platform in active:
        active.discard(platform)
    else:
        active.add(platform)
    try:
        service.update_settings({"publishing": {"platforms": sorted(active)}})
        if update.callback_query is not None:
            await update.callback_query.answer()
    except ConfigError as exc:
        if update.callback_query is not None:
            await update.callback_query.answer(str(exc), show_alert=True)
    await platforms_edit(update, context)


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth.guard(update, auth.Role.VIEWER, action="settings:show"):
        return
    settings = await _settings()
    await errors.deliver(update, context, _msg_settings(settings), _kb_settings())


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, cb: Callback) -> None:
    if cb.action == "show":
        await show(update, context)
        return
    if cb.action == "section":
        if not await auth.guard(update, auth.Role.VIEWER, action="settings:section"):
            return
        section = cb.args[0] if cb.args else ""
        settings = await _settings()
        edit_action = {"discovery": "d_edit", "schedule": "s_edit", "platforms": "p_edit"}.get(section)
        rows = []
        if edit_action:
            rows.append([InlineKeyboardButton("✏️ Edit", callback_data=callbacks.build("settings", edit_action))])
        rows.append(navigation.nav_row(
            refresh=callbacks.build("settings", "section", section),
            back=callbacks.build("settings", "show")))
        await errors.deliver(update, context, _section_detail(settings, section), navigation.kb(*rows))
        return
    if cb.action == "prompt_cancel":
        await prompt_cancel(update, context)
        return

    if cb.action == "d_edit":
        await discovery_edit(update, context)
        return
    if cb.action == "d_topic_add":
        await _prompt(update, context, "topic_add", "Send the topic to add, or /cancel.")
        return
    if cb.action == "d_topic_rm":
        idx = cb.int_arg(0)
        if idx is not None:
            await topic_remove(update, context, idx)
        return
    if cb.action == "d_region":
        await _prompt(update, context, "region", "Send a 2-letter region code (e.g. US), or /cancel.")
        return

    if cb.action == "r_edit":
        await rights_edit(update, context)
        return
    if cb.action == "r_policy":
        policy = cb.args[0] if cb.args else ""
        await rights_policy_confirm_screen(update, context, policy)
        return
    if cb.action == "r_policy_go":
        policy = cb.args[0] if cb.args else ""
        await rights_policy_apply(update, context, policy)
        return
    if cb.action == "r_allow_add":
        await _prompt(update, context, "allow_add", "Send the YouTube channel id to approve, or /cancel.")
        return
    if cb.action == "r_allow_rm":
        idx = cb.int_arg(0)
        if idx is not None:
            await allow_remove(update, context, idx)
        return

    if cb.action == "s_edit":
        await schedule_edit(update, context)
        return
    if cb.action == "s_time_add":
        await _prompt(update, context, "time_add", "Send a publish time as HH:MM (24h), or /cancel.")
        return
    if cb.action == "s_time_rm":
        idx = cb.int_arg(0)
        if idx is not None:
            await time_remove(update, context, idx)
        return
    if cb.action == "s_maxposts":
        await _prompt(update, context, "max_posts", "Send the new max posts/day, or /cancel.")
        return
    if cb.action == "s_maxsources":
        await _prompt(update, context, "max_sources", "Send the new max sources/day, or /cancel.")
        return

    if cb.action == "p_edit":
        await platforms_edit(update, context)
        return
    if cb.action == "p_toggle":
        platform = cb.args[0] if cb.args else ""
        await platform_toggle(update, context, platform)
        return

    await update.callback_query.answer("Unknown action.")


router.register("settings", _on_callback)
