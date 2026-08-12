import React, { useState } from 'react';
import {
    Activity, AlertTriangle, Calendar, CheckCircle2, ChevronDown, ChevronRight,
    Circle, Clock, ExternalLink, HardDrive, Loader2, Pause, Play, Power,
    RefreshCw, Search, SkipForward, Sparkles, XCircle, Zap,
} from 'lucide-react';
import ScoreBreakdown from './ScoreBreakdown';
import {
    badgeClass, explainReason, fmtCount, fmtDuration, fmtInZone, relative,
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

function PublishRow({ attempt, timezone, onRetry, onForceRetry, onResolve, busy }) {
    return (
        <div className="flex items-start gap-3 py-3 border-b border-rule last:border-0">
            <div className="shrink-0 mt-0.5">
                {attempt.state === 'SUBMITTED' && <CheckCircle2 size={15} className="text-ok" />}
                {attempt.state === 'PENDING' && <Circle size={15} className="text-muted" />}
                {attempt.state === 'IN_FLIGHT' && (
                    <Loader2 size={15} className="text-brass animate-spin" />
                )}
                {attempt.state === 'FAILED' && <XCircle size={15} className="text-danger" />}
                {attempt.state === 'UNCERTAIN' && (
                    <AlertTriangle size={15} className="text-warn" />
                )}
                {attempt.state === 'CANCELED' && <XCircle size={15} className="text-muted" />}
            </div>

            <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm text-ink truncate max-w-[24rem]">
                        {attempt.title || `clip ${attempt.clip_index + 1}`}
                    </span>
                    <span className={badgeClass(attempt.state)}>
                        {attempt.state.replace(/_/g, ' ').toLowerCase()}
                    </span>
                </div>
                <div className="flex items-center gap-3 mt-1 flex-wrap">
                    <span className="readout">
                        {fmtInZone(attempt.scheduled_for_utc, attempt.timezone || timezone)}
                    </span>
                    <span className="readout">{attempt.platforms.join(' · ')}</span>
                    {attempt.retry_count > 0 && (
                        <span className="readout text-warn">{attempt.retry_count} retries</span>
                    )}
                </div>
                {attempt.error && (
                    <p className="text-xs text-danger mt-1 break-words">{attempt.error}</p>
                )}

                {attempt.state === 'UNCERTAIN' && (
                    <div className="mt-2 p-3 bg-warn/10 rounded-input">
                        <p className="text-xs text-warn leading-relaxed">
                            The upload was interrupted and Upload-Post never confirmed it.
                            Check your calendar: this clip may already be scheduled.
                            Klippo will not retry on its own, because that could post it twice.
                        </p>
                        <div className="flex flex-wrap gap-2 mt-2">
                            <button
                                onClick={() => onResolve(attempt)}
                                disabled={busy}
                                className="btn-quiet px-3 py-1.5 text-xs"
                            >
                                it is on the calendar
                            </button>
                            <button
                                onClick={() => onForceRetry(attempt)}
                                disabled={busy}
                                className="btn-quiet px-3 py-1.5 text-xs"
                            >
                                it is not — send it again
                            </button>
                        </div>
                    </div>
                )}
            </div>

            {attempt.state === 'FAILED' && (
                <button
                    onClick={() => onRetry(attempt)}
                    disabled={busy}
                    className="btn-quiet px-3 py-1.5 text-xs shrink-0"
                >
                    <RefreshCw size={12} /> retry
                </button>
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
                        'Emergency stop turns Autopilot off and cancels every post it has not sent yet. '
                        + 'Posts already accepted by Upload-Post stay on your calendar — cancel those there.',
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
                        {today.posts_submitted ?? 0} submitted today
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
                    <span className="readout">
                        youtube quota {status.youtube_quota?.units_used_today ?? 0}
                        /{status.youtube_quota?.daily_budget ?? '?'} units
                    </span>
                    {status.youtube_quota?.blocked_until && (
                        <span className="readout text-warn">
                            quota exhausted until {fmtInZone(status.youtube_quota.blocked_until, tz)}
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
