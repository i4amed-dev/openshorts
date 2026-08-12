// Shared formatting for the Autopilot views.
//
// Every timestamp the backend sends is UTC ISO-8601; the operator's timezone is
// part of the Autopilot settings, not the browser's. Rendering with the browser
// zone would make a Madrid schedule read as New York time to someone travelling,
// which is exactly the confusion the backend's stored zone exists to prevent.

export function fmtInZone(iso, timeZone, opts = {}) {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString('en-GB', {
            timeZone: timeZone || 'UTC',
            day: '2-digit', month: 'short',
            hour: '2-digit', minute: '2-digit',
            hour12: false,
            ...opts,
        });
    } catch {
        return iso;
    }
}

export function fmtTimeInZone(iso, timeZone) {
    return fmtInZone(iso, timeZone, { day: undefined, month: undefined });
}

export function relative(iso) {
    if (!iso) return '—';
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return '—';
    const seconds = Math.round((then - Date.now()) / 1000);
    const abs = Math.abs(seconds);
    const [value, unit] =
        abs < 60 ? [seconds, 'second'] :
        abs < 3600 ? [Math.round(seconds / 60), 'minute'] :
        abs < 86400 ? [Math.round(seconds / 3600), 'hour'] :
        [Math.round(seconds / 86400), 'day'];
    try {
        return new Intl.RelativeTimeFormat('en', { numeric: 'auto' }).format(value, unit);
    } catch {
        return `${value} ${unit}s`;
    }
}

export function fmtDuration(seconds) {
    if (!seconds) return '—';
    const total = Math.round(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return h > 0
        ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
        : `${m}:${String(s).padStart(2, '0')}`;
}

export function fmtCount(n) {
    if (n === null || n === undefined) return '—';
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
    return String(n);
}

// Machine-readable rejection reasons → what an operator would actually say.
// Anything unmapped falls back to the raw slug rather than a generic string:
// a reason you cannot look up is worse than an ugly one.
const REASONS = {
    duplicate_source: 'already processed',
    duration_below_minimum: 'too short',
    duration_above_maximum: 'too long',
    older_than_age_window: 'outside the age window',
    views_below_minimum: 'not enough views',
    view_velocity_below_minimum: 'not gaining views fast enough',
    engagement_below_minimum: 'low engagement',
    live_broadcast: 'live stream',
    upcoming_broadcast: 'premiere / not out yet',
    not_public_or_unavailable: 'not publicly available',
    age_restricted: 'restricted or not embeddable',
    made_for_kids: 'made for kids',
    captions_required: 'no captions',
    definition_below_minimum: 'below the minimum quality',
    channel_denylisted: 'channel is on the denylist',
    channel_not_allowlisted: 'channel is not approved',
    channel_cooldown: 'channel used too recently',
    rights_policy: 'licence does not meet your rights policy',
    keyword_excluded: 'matched an excluded keyword',
    keyword_required: 'missing a required keyword',
    source_quality_below_minimum: 'source resolution too low',
    no_clips_generated: 'the pipeline produced no clips',
    daily_source_limit_reached: 'daily source limit reached',
    skipped_by_operator: 'you skipped it',
    emergency_stop: 'emergency stop',
    no_clip_met_the_duration_rules: 'no clip matched your duration rules',
    beyond_max_clips_per_source: 'beyond the per-source clip limit',
    clip_shorter_than_minimum: 'clip too short',
    clip_longer_than_maximum: 'clip too long',
    clip_file_missing: 'clip file missing',
    missed_slot_skipped: 'missed its slot',
    publish_failed: 'publishing failed',
};

export function explainReason(reason) {
    if (!reason) return '';
    return REASONS[reason] || reason.replace(/_/g, ' ');
}

export const STATE_TONE = {
    DISCOVERED: 'muted', ELIGIBLE: 'brass', SELECTED: 'brass',
    PROCESS_QUEUED: 'brass', PROCESSING: 'brass', PROCESS_READY: 'brass',
    CLIPS_SCHEDULED: 'brass', DONE: 'ok',
    FILTERED: 'muted', SKIPPED: 'muted',
    PROCESS_FAILED: 'warn', FAILED: 'danger',
    // Publish lifecycle. SUBMITTED is deliberately NOT green: the vendor has
    // accepted the job, nothing is live yet, and colouring it as success is
    // exactly the lie this pass removed.
    PENDING: 'muted', IN_FLIGHT: 'brass', SCHEDULED: 'brass',
    SUBMITTED: 'brass', PUBLISHING: 'brass',
    PUBLISHED: 'ok', PARTIAL: 'warn', PARTIAL_FAILED: 'warn',
    UNCERTAIN: 'warn', CANCELED: 'muted',
};

// What each publish state means in the operator's language. "Submitted" alone
// reads as done; "scheduled with Upload-Post" does not.
export const PUBLISH_LABEL = {
    PENDING: 'queued locally',
    IN_FLIGHT: 'uploading',
    SUBMITTED: 'scheduled with upload-post',
    PUBLISHING: 'publishing now',
    PUBLISHED: 'published',
    PARTIAL_FAILED: 'partial failure',
    FAILED: 'failed',
    UNCERTAIN: 'needs attention',
    CANCELED: 'canceled',
};

export const PUBLISH_EXPLAINER = {
    SUBMITTED: 'Upload-Post has accepted this post and will publish it at its slot. '
        + 'Nothing is live yet.',
    PUBLISHING: 'Upload-Post is pushing this to the platforms right now.',
    PUBLISHED: 'Upload-Post confirmed every selected platform succeeded.',
    PARTIAL_FAILED: 'Live on some platforms and failed on others. Klippo will not resend it — '
        + 'that would post it twice where it already succeeded.',
    UNCERTAIN: 'The outcome is unknown. Klippo will not resend it on its own.',
};

export function publishLabel(state) {
    return PUBLISH_LABEL[state] || (state || '').replace(/_/g, ' ').toLowerCase();
}

export function badgeClass(state) {
    const tone = STATE_TONE[state] || 'muted';
    return tone === 'muted' ? 'readout' : `badge-${tone}`;
}
