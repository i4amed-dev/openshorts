import React from 'react';
import {
    Activity, AlertTriangle, Calendar, CheckCircle2, ChevronRight,
    Circle, Clock, ExternalLink, HardDrive, Loader2, Search,
    Sparkles, Zap,
} from 'lucide-react';
import {
    badgeClass, fmtCount, fmtDuration, fmtInZone, fmtTimeInZone,
    publishLabel, relative,
} from './format';

// ─── helpers ──────────────────────────────────────────────────────────────────

function PulsingDot({ color = 'ok' }) {
    return (
        <span className="relative flex h-2.5 w-2.5">
            <span className={`animate-ping absolute inline-flex h-full w-full rounded-full bg-${color} opacity-60`} />
            <span className={`relative inline-flex rounded-full h-2.5 w-2.5 bg-${color}`} />
        </span>
    );
}

function SectionHeader({ icon, label, aside }) {
    const HeaderIcon = icon;
    return (
        <div className="flex items-center gap-2 mb-3">
            <HeaderIcon size={14} className="text-brass shrink-0" />
            <h3 className="eyebrow">{label}</h3>
            {aside && <span className="readout ml-auto">{aside}</span>}
        </div>
    );
}

function QuotaBar({ label, used, budget, blockedUntil, tz }) {
    const pct = budget ? Math.min(100, Math.round((used / budget) * 100)) : 0;
    const warn = pct >= 80;
    return (
        <div>
            <div className="flex justify-between items-baseline mb-1">
                <span className="readout">{label}</span>
                <span className={`readout ${warn ? 'text-warn' : ''}`}>
                    {used} / {budget ?? '?'}
                </span>
            </div>
            <div className="h-1 bg-paper3 rounded-full overflow-hidden">
                <div
                    className={`h-full rounded-full transition-all ${warn ? 'bg-warn' : 'bg-brass'}`}
                    style={{ width: `${pct}%` }}
                />
            </div>
            {blockedUntil && (
                <p className="readout text-warn mt-0.5">
                    blocked until {fmtInZone(blockedUntil, tz)}
                </p>
            )}
        </div>
    );
}

// ─── pipeline stage ──────────────────────────────────────────────────────────

const STAGES = [
    { key: 'discovery', label: 'discover', icon: Search },
    { key: 'processing', label: 'process', icon: Zap },
    { key: 'scheduling', label: 'schedule', icon: Calendar },
    { key: 'publishing', label: 'publish', icon: Sparkles },
];

function stageFromStatus(status) {
    const s = status?.status;
    const stage = status?.stage || '';
    if (s === 'OFF' || s === 'IDLE') return null;
    if (stage.toLowerCase().includes('discover')) return 'discovery';
    if (status?.current_source) {
        const src = status.current_source.state || '';
        if (['PROCESS_QUEUED', 'PROCESSING', 'PROCESS_READY'].includes(src)) return 'processing';
        if (['CLIPS_SCHEDULED'].includes(src)) return 'scheduling';
    }
    const pending = status?.publish_attempts?.some(
        (a) => ['IN_FLIGHT', 'PUBLISHING'].includes(a.state));
    if (pending) return 'publishing';
    return null;
}

function Pipeline({ status }) {
    const active = stageFromStatus(status);
    return (
        <div className="flex items-center gap-0">
            {STAGES.map((stage, i) => {
                const Icon = stage.icon;
                const isActive = active === stage.key;
                return (
                    <React.Fragment key={stage.key}>
                        <div className={`flex flex-col items-center gap-1.5 flex-1 py-3 px-2 rounded-lg transition-colors ${
                            isActive ? 'bg-brass/10' : ''}`}>
                            <div className="relative">
                                <Icon
                                    size={16}
                                    className={isActive ? 'text-brass' : 'text-muted'}
                                />
                                {isActive && (
                                    <span className="absolute -top-0.5 -right-0.5 flex h-2 w-2">
                                        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-brass opacity-60" />
                                        <span className="relative inline-flex rounded-full h-2 w-2 bg-brass" />
                                    </span>
                                )}
                            </div>
                            <span className={`eyebrow text-[10px] ${isActive ? 'text-brass' : 'text-muted'}`}>
                                {stage.label}
                            </span>
                        </div>
                        {i < STAGES.length - 1 && (
                            <ChevronRight size={12} className="text-rule shrink-0" />
                        )}
                    </React.Fragment>
                );
            })}
        </div>
    );
}

// ─── now card ────────────────────────────────────────────────────────────────

function NowCard({ status }) {
    const s = status?.status;
    const src = status?.current_source;
    const isRunning = s === 'RUNNING';
    const isError = s === 'PAUSED_ERROR';

    return (
        <div className={`card p-5 border ${
            isError ? 'border-danger/40' :
            isRunning ? 'border-ok/30' :
            'border-rule'}`}>
            <div className="flex items-start gap-3">
                <div className="mt-0.5 shrink-0">
                    {isRunning && <PulsingDot color="ok" />}
                    {isError && <AlertTriangle size={14} className="text-danger" />}
                    {!isRunning && !isError && <Circle size={10} className="text-muted mt-0.5" />}
                </div>
                <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                        <span className={`eyebrow ${
                            isRunning ? 'text-ok' :
                            isError ? 'text-danger' : 'text-muted'}`}>
                            {isRunning ? 'running now' : isError ? 'stopped' : s?.toLowerCase() || 'off'}
                        </span>
                        {status?.pause_requested && !isError && (
                            <span className="badge-warn">pausing after this job</span>
                        )}
                    </div>

                    {src ? (
                        <>
                            <p className="text-sm text-ink leading-snug truncate max-w-lg">
                                {src.title}
                            </p>
                            <div className="flex flex-wrap items-center gap-3 mt-1.5">
                                <span className="readout">{src.channel}</span>
                                <span className="readout">{fmtDuration(src.duration_seconds)}</span>
                                <span className="readout">{fmtCount(src.views)} views</span>
                                <span className={badgeClass(src.state)}>
                                    {src.state?.replace(/_/g, ' ').toLowerCase()}
                                </span>
                                {src.selected_at && (
                                    <span className="readout">started {relative(src.selected_at)}</span>
                                )}
                            </div>
                        </>
                    ) : (
                        <p className="text-sm text-ink2">{status?.stage || 'No active job.'}</p>
                    )}

                    {isError && (
                        <p className="text-xs text-danger mt-1.5 break-words">
                            {status.paused_reason}
                        </p>
                    )}
                </div>

                {src?.url && (
                    <a
                        href={src.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="btn-quiet px-3 py-1.5 text-xs shrink-0 no-underline"
                    >
                        <ExternalLink size={12} />
                    </a>
                )}
            </div>
        </div>
    );
}

// ─── next in queue ───────────────────────────────────────────────────────────

function NextCard({ source }) {
    if (!source) {
        return (
            <div className="card p-4">
                <SectionHeader icon={Clock} label="up next" />
                <p className="text-xs text-muted">Nothing in the queue. Discovery will look again soon.</p>
            </div>
        );
    }
    return (
        <div className="card p-4">
            <SectionHeader icon={Clock} label="up next" aside={`score ${source.score?.toFixed(1)}`} />
            <p className="text-sm text-ink truncate">{source.title}</p>
            <div className="flex flex-wrap gap-3 mt-1.5">
                <span className="readout">{source.channel}</span>
                <span className="readout">{fmtDuration(source.duration_seconds)}</span>
                <span className="readout">{fmtCount(source.views)} views</span>
                <span className="readout text-ok">
                    {source.license === 'creativeCommon' ? 'creative commons' : source.license}
                </span>
            </div>
        </div>
    );
}

// ─── publish timeline ─────────────────────────────────────────────────────────

const PUBLISH_ICON = {
    PUBLISHED: CheckCircle2,
    FAILED: AlertTriangle,
    IN_FLIGHT: Loader2,
    PUBLISHING: Loader2,
};

function PublishTimeline({ attempts, tz }) {
    if (!attempts?.length) {
        return (
            <div className="card p-4">
                <SectionHeader icon={Calendar} label="schedule" />
                <p className="text-xs text-muted">No posts scheduled yet.</p>
            </div>
        );
    }

    return (
        <div className="card p-4">
            <SectionHeader
                icon={Calendar}
                label="schedule"
                aside={`${attempts.length} post${attempts.length !== 1 ? 's' : ''}`}
            />
            <div className="space-y-2">
                {attempts.map((a) => {
                    const Icon = PUBLISH_ICON[a.state] || Circle;
                    const isSpinning = ['IN_FLIGHT', 'PUBLISHING'].includes(a.state);
                    return (
                        <div key={a.id} className="flex items-center gap-3">
                            <Icon
                                size={13}
                                className={`shrink-0 ${
                                    a.state === 'PUBLISHED' ? 'text-ok' :
                                    a.state === 'FAILED' ? 'text-danger' :
                                    isSpinning ? 'text-brass animate-spin' : 'text-muted'}`}
                            />
                            <div className="flex-1 min-w-0">
                                <p className="text-xs text-ink truncate">{a.title || `clip ${a.clip_index + 1}`}</p>
                                <div className="flex gap-2 mt-0.5">
                                    <span className="readout">
                                        {fmtTimeInZone(a.scheduled_for_utc, a.timezone || tz)}
                                    </span>
                                    <span className="readout">{(a.platforms || []).join(' · ')}</span>
                                </div>
                            </div>
                            <span className={`${badgeClass(a.state)} shrink-0`}>
                                {publishLabel(a.state)}
                            </span>
                        </div>
                    );
                })}
            </div>
        </div>
    );
}

// ─── live event feed ──────────────────────────────────────────────────────────

const LEVEL_STYLE = {
    error: 'text-danger',
    warn: 'text-warn',
    info: 'text-ink2',
    debug: 'text-muted',
};

function EventFeed({ events, tz, lastTickAt }) {
    return (
        <div className="card p-4">
            <SectionHeader
                icon={Activity}
                label="live feed"
                aside={lastTickAt ? `last tick ${relative(lastTickAt)}` : undefined}
            />
            <div className="space-y-1.5 max-h-64 overflow-y-auto custom-scrollbar">
                {events?.length ? events.map((e) => (
                    <div key={e.id} className="flex gap-2 items-start">
                        <span className="readout shrink-0 w-12 text-right">
                            {fmtTimeInZone(e.ts, tz)}
                        </span>
                        <span className="readout shrink-0 w-16 truncate">{e.stage}</span>
                        <span className={`text-xs break-words leading-snug ${LEVEL_STYLE[e.level] || 'text-ink2'}`}>
                            {e.message}
                        </span>
                    </div>
                )) : (
                    <p className="text-xs text-muted">No events yet.</p>
                )}
            </div>
        </div>
    );
}

// ─── system health ────────────────────────────────────────────────────────────

function HealthCard({ status }) {
    const q = status?.youtube_quota || {};
    const store = status?.storage || {};
    const tz = status?.timezone || 'UTC';
    const today = status?.today || {};

    return (
        <div className="card p-4 space-y-4">
            <SectionHeader icon={HardDrive} label="system" />

            <div className="grid grid-cols-2 gap-3">
                <div className="text-center p-3 bg-paper2 rounded-input">
                    <p className="font-display text-xl text-ink">{today.sources_selected ?? 0}
                        <span className="text-muted text-sm"> / {today.max_sources ?? '?'}</span>
                    </p>
                    <p className="eyebrow mt-1">sources today</p>
                </div>
                <div className="text-center p-3 bg-paper2 rounded-input">
                    <p className="font-display text-xl text-ink">{today.posts_scheduled ?? 0}
                        <span className="text-muted text-sm"> / {today.max_posts ?? '?'}</span>
                    </p>
                    <p className="eyebrow mt-1">posts today</p>
                </div>
            </div>

            <div className="space-y-3">
                <QuotaBar
                    label="youtube api units"
                    used={q.general_units_used ?? 0}
                    budget={q.general_budget}
                    blockedUntil={q.general_blocked_until}
                    tz={tz}
                />
                <QuotaBar
                    label="search queries"
                    used={q.search_calls_used ?? 0}
                    budget={q.search_budget}
                    blockedUntil={q.search_blocked_until}
                    tz={tz}
                />
            </div>

            {store.available && (
                <div>
                    <div className="flex justify-between items-baseline mb-1">
                        <span className="readout">disk</span>
                        <span className={`readout ${store.low ? 'text-warn' : ''}`}>
                            {store.free_gb} GB free of {store.total_gb} GB
                        </span>
                    </div>
                    <div className="h-1 bg-paper3 rounded-full overflow-hidden">
                        <div
                            className={`h-full rounded-full transition-all ${store.low ? 'bg-warn' : 'bg-ok'}`}
                            style={{ width: `${Math.min(100, Math.round(((store.total_gb - store.free_gb) / store.total_gb) * 100))}%` }}
                        />
                    </div>
                </div>
            )}

            <div className="flex flex-wrap gap-x-3 gap-y-1 pt-1 border-t border-rule">
                <span className="readout">tz {tz}</span>
                {status?.next_discovery_at && (
                    <span className="readout">
                        next discovery {relative(status.next_discovery_at)}
                    </span>
                )}
                {status?.next_publish_at && (
                    <span className="readout">
                        next post {fmtTimeInZone(status.next_publish_at, tz)}
                    </span>
                )}
            </div>
        </div>
    );
}

// ─── root ─────────────────────────────────────────────────────────────────────

export default function AutopilotMonitor({ status }) {
    if (!status) {
        return (
            <div className="flex items-center gap-2 text-muted text-sm py-12 justify-center">
                <Loader2 size={16} className="animate-spin" /> loading…
            </div>
        );
    }

    const tz = status.timezone || 'UTC';
    const nextInQueue = status.queue?.[0] || null;

    return (
        <div className="space-y-4">
            {/* pipeline */}
            <div className="card px-4 py-2">
                <Pipeline status={status} />
            </div>

            {/* now */}
            <NowCard status={status} />

            {/* next + schedule side by side on wider screens */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <NextCard source={nextInQueue} />
                <PublishTimeline attempts={status.publish_attempts} tz={tz} />
            </div>

            {/* feed + health side by side */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                <EventFeed events={status.events} tz={tz} lastTickAt={status.last_tick_at} />
                <HealthCard status={status} />
            </div>
        </div>
    );
}
