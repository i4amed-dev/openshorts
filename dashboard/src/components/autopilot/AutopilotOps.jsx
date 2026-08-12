import React, { useState } from 'react';
import {
    Activity, AlertTriangle, Calendar, CheckCircle2, ChevronDown, ChevronRight,
    Circle, Clock, ExternalLink, HardDrive, Loader2, Pause, Play, Power,
    RefreshCw, Search, SkipForward, Sparkles, XCircle, Zap,
} from 'lucide-react';
import ScoreBreakdown from './ScoreBreakdown';
import {
    PUBLISH_EXPLAINER, badgeClass, explainReason, fmtCount, fmtDuration, fmtInZone,
    publishLabel, relative,
} from './format';

/**
 * The Autopilot operations view.
 *
 * Built around one question: someone opens Klippo after two days away — can they
 * immediately say what it found, what it chose and why, what is running, what is
 * scheduled, what failed, and what happens next? Every block below answers one
 * of those, in that order.
 */

const STATUS_COPY = {
    OFF: { label: 'off', tone: 'readout', icon: Power },
    IDLE: { label: 'idle', tone: 'badge-brass', icon: Clock },
    RUNNING: { label: 'running', tone: 'badge-ok', icon: Activity },
    PAUSED: { label: 'paused', tone: 'badge-warn', icon: Pause },
    PAUSED_ERROR: { label: 'stopped · errors', tone: 'badge-danger', icon: AlertTriangle },
};

function Stat({ label, value, of, tone = 'text-ink' }) {
    return (
        <div className="card p-4">
            <p className="eyebrow mb-2">{label}</p>
            <p className={`font-display text-2xl ${tone}`}>
                {value}
                {of !== undefined && of !== null && (
                    <span className="text-muted text-base"> / {of}</span>
                )}
            </p>
        </div>
    );
}

function SourceRow({ source, onSkip, onRetry, busy }) {
    const [open, setOpen] = useState(false);
    return (
        <div className="border-b border-rule last:border-0">
            <div className="flex items-start gap-3 py-3">
                <button
                    onClick={() => setOpen(!open)}
                    className="mt-0.5 text-muted hover:text-ink transition-colors shrink-0"
                    aria-label={open ? 'hide details' : 'show details'}
                >
                    {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                </button>

                <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-sm text-ink truncate max-w-[28rem]">
                            {source.title || source.video_id}
                        </span>
                        <span className={badgeClass(source.state)}>
                            {source.state.replace(/_/g, ' ').toLowerCase()}
                        </span>
                    </div>
                    <div className="flex items-center gap-3 mt-1 flex-wrap">
                        <span className="readout">{source.channel || 'unknown channel'}</span>
                        <span className="readout">{fmtCount(source.views)} views</span>
                        <span className="readout">{fmtDuration(source.duration_seconds)}</span>
                        {source.license === 'creativeCommon' && (
                            <span className="readout text-ok">creative commons</span>
                        )}
                        {source.rejection_reason && (
                            <span className="readout text-warn">
                                {explainReason(source.rejection_reason)}
                            </span>
                        )}
                        {source.next_retry_at && (
                            <span className="readout text-warn">
                                retries {relative(source.next_retry_at)}
                            </span>
                        )}
                    </div>
                </div>

                <div className="shrink-0 text-right">
                    <p className="font-mono text-sm text-brass">{source.score?.toFixed(1)}</p>
                    <p className="readout">score</p>
                </div>
            </div>

            {open && (
                <div className="pb-4 pl-8 pr-2 space-y-3 animate-fade">
                    <ScoreBreakdown breakdown={source.score_breakdown} />

                    <div className="flex flex-wrap gap-x-4 gap-y-1">
                        <span className="readout">found via {source.discovery_source}</span>
                        <span className="readout">{relative(source.discovered_at)}</span>
                        {source.rights_policy && (
                            <span className="readout">
                                policy: {source.rights_policy.replace(/_/g, ' ').toLowerCase()}
                            </span>
                        )}
                        {source.job_id && (
                            <span className="readout">job {source.job_id.slice(0, 8)}</span>
                        )}
                        {source.attempts > 0 && (
                            <span className="readout">{source.attempts} attempt(s)</span>
                        )}
                    </div>

                    {source.last_error && (
                        <p className="text-xs text-danger break-words">{source.last_error}</p>
                    )}

                    <div className="flex flex-wrap gap-2">
                        <a
                            href={source.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="btn-quiet px-3 py-1.5 text-xs no-underline"
                        >
                            <ExternalLink size={12} /> watch on youtube
                        </a>
                        {onSkip && (
                            <button
                                onClick={() => onSkip(source)}
                                disabled={busy}
                                className="btn-quiet px-3 py-1.5 text-xs"
                            >
                                <SkipForward size={12} /> skip
                            </button>
                        )}
                        {onRetry && (
                            <button
                                onClick={() => onRetry(source)}
                                disabled={busy}
                                className="btn-quiet px-3 py-1.5 text-xs"
                            >
                                <RefreshCw size={12} /> retry
                            </button>
                        )}
                    </div>
                </div>
            )}
        </div>
    );
}

function PublishRow({ attempt, timezone, onRetry, onForceRetry, onResolve, onAbandon, busy }) {
    const [open, setOpen] = useState(false);
    const state = attempt.state;
    const results = attempt.vendor_results || [];
    const needsAttention = state === 'UNCERTAIN' || state === 'PARTIAL_FAILED';

    return (
        <div className="py-3 border-b border-rule last:border-0">
            <div className="flex items-start gap-3">
                <div className="shrink-0 mt-0.5">
                    {state === 'PUBLISHED' && <CheckCircle2 size={15} className="text-ok" />}
                    {state === 'PENDING' && <Circle size={15} className="text-muted" />}
                    {(state === 'IN_FLIGHT' || state === 'PUBLISHING') && (
                        <Loader2 size={15} className="text-brass animate-spin" />
                    )}
                    {state === 'SUBMITTED' && <Clock size={15} className="text-brass" />}
                    {state === 'FAILED' && <XCircle size={15} className="text-danger" />}
                    {(state === 'UNCERTAIN' || state === 'PARTIAL_FAILED') && (
                        <AlertTriangle size={15} className="text-warn" />
                    )}
                    {state === 'CANCELED' && <XCircle size={15} className="text-muted" />}
                </div>

                <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-sm text-ink truncate max-w-[22rem]">
                            {attempt.title || `clip ${attempt.clip_index + 1}`}
                        </span>
                        <span className={badgeClass(state)}>{publishLabel(state)}</span>
                    </div>
                    <div className="flex items-center gap-3 mt-1 flex-wrap">
                        <span className="readout">
                            {fmtInZone(attempt.scheduled_for_utc, attempt.timezone || timezone)}
                        </span>
                        <span className="readout">{(attempt.platforms || []).join(' · ')}</span>
                        {attempt.retry_count > 0 && (
                            <span className="readout text-warn">{attempt.retry_count} retries</span>
                        )}
                        {attempt.vendor_tracking_id && (
                            <span className="readout">id {attempt.vendor_tracking_id}</span>
                        )}
                        {attempt.last_status_check_at && (
                            <span className="readout">
                                checked {relative(attempt.last_status_check_at)}
                            </span>
                        )}
                    </div>

                    {PUBLISH_EXPLAINER[state] && (
                        <p className="text-xs text-muted mt-1.5 leading-relaxed">
                            {PUBLISH_EXPLAINER[state]}
                        </p>
                    )}

                    {/* Per-platform outcomes: the only honest way to show a
                        multi-platform post, where one network can fail alone. */}
                    {results.length > 0 && (
                        <div className="flex flex-wrap gap-2 mt-2">
                            {results.map((r) => (
                                <span
                                    key={r.platform}
                                    className={`readout px-2 py-0.5 rounded-input ${
                                        r.status === 'completed' ? 'text-ok bg-ok/10'
                                            : r.status === 'failed' ? 'text-danger bg-danger/10'
                                            : 'text-muted bg-paper3'}`}
                                    title={r.message || r.status}
                                >
                                    {r.platform} · {r.status}
                                </span>
                            ))}
                        </div>
                    )}

                    {attempt.error && (
                        <p className="text-xs text-danger mt-1.5 break-words">{attempt.error}</p>
                    )}

                    {needsAttention && (
                        <div className="mt-2 p-3 bg-warn/10 rounded-input">
                            <p className="text-xs text-warn leading-relaxed">
                                {state === 'PARTIAL_FAILED' ? (
                                    <>
                                        This went live on <strong>
                                            {results.filter((r) => r.status === 'completed')
                                                .map((r) => r.platform).join(', ') || 'some platforms'}
                                        </strong> and failed on <strong>
                                            {results.filter((r) => r.status === 'failed')
                                                .map((r) => r.platform).join(', ') || 'others'}
                                        </strong>. Resending would publish it twice where it
                                        already succeeded, so Klippo will not retry.
                                        Post the failed platform by hand, then close this out.
                                    </>
                                ) : (
                                    <>
                                        Klippo could not establish what happened to this post.
                                        Check your Upload-Post calendar before deciding —
                                        it will not resend on its own.
                                    </>
                                )}
                            </p>
                            <div className="flex flex-wrap gap-2 mt-2">
                                <button onClick={() => onResolve(attempt)} disabled={busy}
                                    className="btn-quiet px-3 py-1.5 text-xs">
                                    it is live — mark published
                                </button>
                                {state === 'UNCERTAIN' && (
                                    <button onClick={() => onForceRetry(attempt)} disabled={busy}
                                        className="btn-quiet px-3 py-1.5 text-xs">
                                        it never posted — send again
                                    </button>
                                )}
                                <button onClick={() => onAbandon(attempt)} disabled={busy}
                                    className="btn-quiet px-3 py-1.5 text-xs">
                                    give up on it
                                </button>
                            </div>
                        </div>
                    )}
                </div>

                <div className="shrink-0 flex flex-col items-end gap-1">
                    {state === 'FAILED' && (
                        <button onClick={() => onRetry(attempt)} disabled={busy}
                            className="btn-quiet px-3 py-1.5 text-xs">
                            <RefreshCw size={12} /> retry
                        </button>
                    )}
                    {(attempt.vendor_status || attempt.next_status_check_at) && (
                        <button onClick={() => setOpen(!open)}
                            className="readout hover:text-ink transition-colors">
                            {open ? 'less' : 'details'}
                        </button>
                    )}
                </div>
            </div>

            {open && (
                <div className="mt-2 ml-8 flex flex-wrap gap-x-4 gap-y-1 animate-fade">
                    {attempt.vendor_status && (
                        <span className="readout">vendor says: {attempt.vendor_status}</span>
                    )}
                    {attempt.vendor_is_scheduled && (
                        <span className="readout">held as a scheduled job</span>
                    )}
                    {attempt.next_status_check_at && (
                        <span className="readout">
                            next check {relative(attempt.next_status_check_at)}
                        </span>
                    )}
                    {attempt.submitted_at && (
                        <span className="readout">accepted {relative(attempt.submitted_at)}</span>
                    )}
                    {attempt.finalized_at && (
                        <span className="readout">settled {relative(attempt.finalized_at)}</span>
                    )}
                </div>
            )}
        </div>
    );
}


export default function AutopilotOps({ status, busy, action, onOpenSetup }) {
    const [confirm, setConfirm] = useState(null);
    const [showRejected, setShowRejected] = useState(false);

    if (!status) {
        return (
            <div className="flex items-center gap-2 text-muted text-sm py-12 justify-center">
                <Loader2 size={16} className="animate-spin" /> loading autopilot…
            </div>
        );
    }

    const meta = STATUS_COPY[status.status] || STATUS_COPY.OFF;
    const StatusIcon = meta.icon;
    const tz = status.timezone;
    const today = status.today || {};
    const creds = status.credentials || {};
    const missingCreds = Object.entries({
        'Gemini API key': creds.gemini,
        'YouTube Data API key': creds.youtube_data_api,
        'Upload-Post API key': creds.upload_post_key,
        'Upload-Post profile': creds.upload_post_user,
    }).filter(([, present]) => !present).map(([name]) => name);

    const run = (name, args) => action(name, args);
    const confirmThen = (message, name, args) => setConfirm({ message, name, args });

    return (
        <div className="space-y-6">
            {/* --- Headline state --------------------------------------------- */}
            <div className="card p-5">
                <div className="flex flex-wrap items-start justify-between gap-4">
                    <div className="min-w-0">
                        <div className="flex items-center gap-3 mb-1.5">
                            <StatusIcon
                                size={18}
                                className={status.status === 'RUNNING' ? 'text-ok'
                                    : status.status === 'PAUSED_ERROR' ? 'text-danger'
                                    : status.status === 'PAUSED' ? 'text-warn'
                                    : status.enabled ? 'text-brass' : 'text-muted'}
                            />
                            <h2 className="font-display lowercase text-xl text-ink">autopilot</h2>
                            <span className={meta.tone}>{meta.label}</span>
                        </div>
                        <p className="text-sm text-ink2">{status.stage}</p>
                        {status.current_source && (
                            <p className="readout mt-1 truncate max-w-lg">
                                {status.current_source.title}
                            </p>
                        )}
                    </div>

                    <div className="flex flex-wrap gap-2">
                        {!status.enabled ? (
                            <button onClick={() => run('enable')} disabled={busy}
                                className="btn-primary px-5 py-2.5 text-sm">
                                <Power size={15} /> enable
                            </button>
                        ) : (
                            <>
                                {status.pause_requested || status.status === 'PAUSED_ERROR' ? (
                                    <button onClick={() => run('resume')} disabled={busy}
                                        className="btn-primary px-5 py-2.5 text-sm">
                                        <Play size={15} /> resume
                                    </button>
                                ) : (
                                    <button onClick={() => run('pause')} disabled={busy}
                                        className="btn-ghost px-4 py-2.5 text-sm">
                                        <Pause size={15} /> pause
                                    </button>
                                )}
                                <button
                                    onClick={() => confirmThen(
                                        'Turn Autopilot off? It will finish nothing new. Posts already submitted to Upload-Post stay on your calendar.',
                                        'disable')}
                                    disabled={busy}
                                    className="btn-ghost px-4 py-2.5 text-sm">
                                    <Power size={15} /> disable
                                </button>
                            </>
                        )}
                        <button onClick={onOpenSetup} className="btn-quiet px-4 py-2.5 text-sm">
                            setup
                        </button>
                    </div>
                </div>

                {status.status === 'PAUSED_ERROR' && (
                    <div className="mt-4 p-3 bg-danger/10 rounded-input flex items-start gap-2">
                        <AlertTriangle size={14} className="text-danger mt-0.5 shrink-0" />
                        <div>
                            <p className="text-sm text-danger">Autopilot stopped itself.</p>
                            <p className="text-xs text-muted mt-0.5">{status.paused_reason}</p>
                        </div>
                    </div>
                )}

                {status.enabled && missingCreds.length > 0 && (
                    <div className="mt-4 p-3 bg-warn/10 rounded-input flex items-start gap-2">
                        <AlertTriangle size={14} className="text-warn mt-0.5 shrink-0" />
                        <p className="text-xs text-warn">
                            Missing server-side credentials: {missingCreds.join(', ')}.
                            Unattended mode cannot read your browser, so these must be set
                            in the backend environment (<code>.env</code>).
                        </p>
                    </div>
                )}

                {status.storage?.low && (
                    <div className="mt-4 p-3 bg-warn/10 rounded-input flex items-start gap-2">
                        <HardDrive size={14} className="text-warn mt-0.5 shrink-0" />
                        <p className="text-xs text-warn">
                            Only {status.storage.free_gb} GB free on the output volume.
                            Long sources need headroom while they render.
                        </p>
                    </div>
                )}
            </div>

            {/* --- What happens next ------------------------------------------ */}
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
                <Stat label="next discovery"
                    value={<span className="text-base">{fmtInZone(status.next_discovery_at, tz)}</span>} />
                <Stat label="next post"
                    value={<span className="text-base">{fmtInZone(status.next_publish_at, tz)}</span>} />
                <Stat label="sources today" value={today.sources_selected ?? 0}
                    of={today.max_sources} />
                <Stat label="posts today" value={today.posts_scheduled ?? 0}
                    of={today.max_posts} />
            </div>

            {/* --- Manual controls -------------------------------------------- */}
            <div className="card p-4 flex flex-wrap items-center gap-2">
                <span className="eyebrow mr-2">run now</span>
                <button onClick={() => run('discover')} disabled={busy}
                    className="btn-quiet px-3 py-2 text-xs">
                    <Search size={13} /> discover
                </button>
                <button onClick={() => run('process-next')} disabled={busy}
                    className="btn-quiet px-3 py-2 text-xs">
                    <Zap size={13} /> process next candidate
                </button>
                <div className="flex-1" />
                <button
                    onClick={() => confirmThen(
                        'Emergency stop turns Autopilot off, drops everything still queued here, and asks '
                        + 'Upload-Post to cancel the future posts it is holding for Klippo. Anything already '
                        + 'published stays published, and a post whose slot has passed is re-checked rather '
                        + 'than assumed cancelled. Manual posts are never touched.',
                        'emergency-stop')}
                    disabled={busy}
                    className="btn-danger px-3 py-2 text-xs">
                    <AlertTriangle size={13} /> emergency stop
                </button>
            </div>

            {/* --- Scheduled posts -------------------------------------------- */}
            <section className="card p-5">
                <div className="flex items-center gap-2 mb-3">
                    <Calendar size={15} className="text-brass" />
                    <h3 className="text-sm text-ink lowercase">scheduled &amp; published</h3>
                    <span className="readout ml-auto">
                        {today.posts_published ?? 0} published · {today.posts_submitted ?? 0} accepted today
                    </span>
                </div>
                {status.publish_attempts?.length ? (
                    <div className="divide-y divide-rule">
                        {status.publish_attempts.map((attempt) => (
                            <PublishRow
                                key={attempt.id}
                                attempt={attempt}
                                timezone={tz}
                                busy={busy}
                                onRetry={(a) => run('retry-publish', a.id)}
                                onForceRetry={(a) => confirmThen(
                                    'Only do this if you checked the Upload-Post calendar and the post is NOT there. '
                                    + 'Sending again when it already exists will publish the clip twice.',
                                    'force-retry-publish', a.id)}
                                onResolve={(a) => run('resolve-publish', a.id)}
                                onAbandon={(a) => confirmThen(
                                    'This stops Klippo tracking the post. Anything already live stays live — '
                                    + 'nothing is removed from any platform.',
                                    'abandon-publish', a.id)}
                            />
                        ))}
                    </div>
                ) : (
                    <p className="text-xs text-muted">Nothing scheduled yet.</p>
                )}
            </section>

            {/* --- Recently chosen -------------------------------------------- */}
            <section className="card p-5">
                <div className="flex items-center gap-2 mb-3">
                    <Sparkles size={15} className="text-brass" />
                    <h3 className="text-sm text-ink lowercase">recently chosen</h3>
                </div>
                {status.recent_selected?.length ? (
                    <div>
                        {status.recent_selected.map((source) => (
                            <SourceRow key={source.id} source={source} busy={busy}
                                onRetry={['FAILED', 'PROCESS_FAILED'].includes(source.state)
                                    ? (s) => run('retry-source', s.id) : undefined} />
                        ))}
                    </div>
                ) : (
                    <p className="text-xs text-muted">Nothing processed yet.</p>
                )}
            </section>

            {/* --- Candidate queue -------------------------------------------- */}
            <section className="card p-5">
                <div className="flex items-center gap-2 mb-3">
                    <Clock size={15} className="text-brass" />
                    <h3 className="text-sm text-ink lowercase">candidate queue</h3>
                    <span className="readout ml-auto">{status.queue?.length || 0} waiting</span>
                </div>
                {status.queue?.length ? (
                    <div>
                        {status.queue.map((source) => (
                            <SourceRow key={source.id} source={source} busy={busy}
                                onSkip={(s) => run('skip-source', s.id)} />
                        ))}
                    </div>
                ) : (
                    <p className="text-xs text-muted">
                        No eligible candidates. The next discovery run will look again.
                    </p>
                )}
            </section>

            {/* --- Rejected ---------------------------------------------------- */}
            <section className="card p-5">
                <button
                    onClick={() => setShowRejected(!showRejected)}
                    className="flex items-center gap-2 w-full text-left"
                >
                    {showRejected ? <ChevronDown size={14} className="text-muted" />
                        : <ChevronRight size={14} className="text-muted" />}
                    <h3 className="text-sm text-ink lowercase">not used</h3>
                    <span className="readout ml-auto">{status.rejected?.length || 0}</span>
                </button>
                {showRejected && (
                    <div className="mt-3">
                        {status.rejected?.length ? status.rejected.map((source) => (
                            <SourceRow key={source.id} source={source} busy={busy} />
                        )) : (
                            <p className="text-xs text-muted">Nothing rejected yet.</p>
                        )}
                    </div>
                )}
            </section>

            {/* --- Errors and activity ---------------------------------------- */}
            {status.recent_errors?.length > 0 && (
                <section className="card p-5">
                    <div className="flex items-center gap-2 mb-3">
                        <AlertTriangle size={15} className="text-danger" />
                        <h3 className="text-sm text-ink lowercase">recent errors</h3>
                    </div>
                    <div className="space-y-2">
                        {status.recent_errors.map((event) => (
                            <div key={event.id} className="flex gap-3 items-start">
                                <span className="readout shrink-0 w-24">
                                    {fmtInZone(event.ts, tz)}
                                </span>
                                <span className="text-xs text-danger break-words">
                                    {event.message}
                                </span>
                            </div>
                        ))}
                    </div>
                </section>
            )}

            <section className="card p-5">
                <div className="flex items-center gap-2 mb-3">
                    <Activity size={15} className="text-brass" />
                    <h3 className="text-sm text-ink lowercase">activity</h3>
                    <span className="readout ml-auto">
                        last tick {relative(status.last_tick_at)}
                    </span>
                </div>
                <div className="space-y-1.5 max-h-72 overflow-y-auto custom-scrollbar">
                    {status.events?.length ? status.events.map((event) => (
                        <div key={event.id} className="flex gap-3 items-start">
                            <span className="readout shrink-0 w-24">{fmtInZone(event.ts, tz)}</span>
                            <span className="readout shrink-0 w-20">{event.stage}</span>
                            <span className={`text-xs break-words ${
                                event.level === 'error' ? 'text-danger'
                                    : event.level === 'warn' ? 'text-warn' : 'text-ink2'}`}>
                                {event.message}
                            </span>
                        </div>
                    )) : <p className="text-xs text-muted">No activity recorded yet.</p>}
                </div>
                <div className="mt-4 pt-3 border-t border-rule flex flex-wrap gap-x-4 gap-y-1">
                    {/* Two independent YouTube allocations — running out of one
                        does not stop the other, so they are shown separately. */}
                    <span className="readout">
                        api units {status.youtube_quota?.general_units_used ?? 0}
                        /{status.youtube_quota?.general_budget ?? '?'}
                    </span>
                    <span className="readout">
                        search queries {status.youtube_quota?.search_calls_used ?? 0}
                        /{status.youtube_quota?.search_budget ?? '?'}
                    </span>
                    {status.youtube_quota?.general_blocked_until && (
                        <span className="readout text-warn">
                            api quota blocked until {fmtInZone(status.youtube_quota.general_blocked_until, tz)}
                        </span>
                    )}
                    {status.youtube_quota?.search_blocked_until && (
                        <span className="readout text-warn">
                            search blocked until {fmtInZone(status.youtube_quota.search_blocked_until, tz)}
                        </span>
                    )}
                    {status.storage?.available && (
                        <span className="readout">
                            {status.storage.free_gb} GB free of {status.storage.total_gb} GB
                        </span>
                    )}
                    <span className="readout">timezone {tz}</span>
                </div>
            </section>

            {/* --- Confirmation ------------------------------------------------ */}
            {confirm && (
                <div
                    className="fixed inset-0 z-[100] flex items-center justify-center bg-black/70 p-4 animate-fade"
                    onMouseDown={(e) => { if (e.target === e.currentTarget) setConfirm(null); }}
                >
                    <div className="card p-6 max-w-md w-full">
                        <p className="eyebrow mb-2">confirm</p>
                        <p className="text-sm text-ink2 leading-relaxed mb-5">{confirm.message}</p>
                        <div className="flex gap-3">
                            <button onClick={() => setConfirm(null)} className="btn-ghost flex-1">
                                cancel
                            </button>
                            <button
                                onClick={() => { run(confirm.name, confirm.args); setConfirm(null); }}
                                className="btn-primary flex-1"
                            >
                                continue
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
