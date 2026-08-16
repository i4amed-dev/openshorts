import React from 'react';
import { AlertTriangle, ListFilter } from 'lucide-react';
import { ageCohortLabel, laneLabel, relative } from './format';

/**
 * "Is discovery healthy?" — the funnel the old dashboard never showed even
 * though the backend always computed it (automation/discovery.py's
 * DiscoveryResult, which used to be written to `status.runs` and never
 * rendered). Fetched → eligible → shortlisted → selected, plus which lanes
 * ran and how ages/scores are distributed, is what turns "nothing was
 * selected" from a guess into a diagnosis.
 */

function FunnelStep({ label, value, of }) {
    const pct = of ? Math.min(100, Math.round((value / Math.max(1, of)) * 100)) : 0;
    return (
        <div className="flex items-center gap-3">
            <span className="readout w-28 shrink-0">{label}</span>
            <div className="flex-1 h-2 rounded-full bg-paper3 overflow-hidden">
                <div
                    className="h-full bg-brass/70 rounded-full transition-all"
                    style={{ width: of ? `${pct}%` : value > 0 ? '100%' : '0%' }}
                />
            </div>
            <span className="font-mono text-xs text-ink w-10 text-right shrink-0">{value}</span>
        </div>
    );
}

function CountRows({ counts, labelFor }) {
    const entries = Object.entries(counts || {}).sort(([, a], [, b]) => b - a);
    if (!entries.length) return <p className="text-xs text-muted">No data yet.</p>;
    const max = Math.max(...entries.map(([, n]) => n), 1);
    return (
        <div className="space-y-1.5">
            {entries.map(([key, n]) => (
                <div key={key} className="flex items-center gap-3">
                    <span className="readout w-40 shrink-0 truncate">{labelFor(key)}</span>
                    <div className="flex-1 h-1.5 rounded-full bg-paper3 overflow-hidden">
                        <div className="h-full bg-brass/50 rounded-full"
                            style={{ width: `${Math.round((n / max) * 100)}%` }} />
                    </div>
                    <span className="font-mono text-xs text-ink w-8 text-right shrink-0">{n}</span>
                </div>
            ))}
        </div>
    );
}

export default function DiscoveryFunnel({ funnel, diagnostic }) {
    if (!funnel) return null;
    const hasData = (funnel.fetched || 0) > 0;

    return (
        <section className="card p-5 space-y-5">
            <div className="flex items-center gap-2">
                <ListFilter size={15} className="text-brass" />
                <h3 className="text-sm text-ink lowercase">discovery funnel</h3>
                {funnel.run_at && (
                    <span className="readout ml-auto">last run {relative(funnel.run_at)}</span>
                )}
            </div>

            {!hasData ? (
                <p className="text-xs text-muted">No discovery run yet.</p>
            ) : (
                <>
                    <div className="space-y-2">
                        <FunnelStep label="fetched" value={funnel.fetched} />
                        <FunnelStep label="stored" value={funnel.stored} of={funnel.fetched} />
                        <FunnelStep label="eligible" value={funnel.eligible} of={funnel.fetched} />
                        <FunnelStep label="shortlisted" value={funnel.shortlisted}
                            of={funnel.fetched} />
                        <FunnelStep label="selected today" value={funnel.selected}
                            of={funnel.fetched} />
                    </div>

                    <div className="grid sm:grid-cols-2 gap-5 pt-1">
                        <div>
                            <p className="eyebrow mb-2">lanes this run</p>
                            {funnel.lanes_run?.length ? (
                                <div className="flex flex-wrap gap-1.5">
                                    {funnel.lanes_run.map((lane) => (
                                        <span key={lane} className="readout px-2 py-0.5 rounded-input bg-paper3">
                                            {laneLabel(lane)}
                                        </span>
                                    ))}
                                </div>
                            ) : <p className="text-xs text-muted">—</p>}
                            <p className="eyebrow mt-4 mb-2">candidates by lane</p>
                            <CountRows counts={funnel.lane_counts} labelFor={laneLabel} />
                        </div>
                        <div>
                            <p className="eyebrow mb-2">candidates by age</p>
                            <CountRows counts={funnel.age_distribution} labelFor={ageCohortLabel} />
                            <div className="flex gap-4 mt-4">
                                <div>
                                    <p className="eyebrow mb-1">avg opportunity</p>
                                    <p className="font-mono text-sm text-ink">
                                        {funnel.average_opportunity?.toFixed(1) ?? '—'}
                                    </p>
                                </div>
                                <div>
                                    <p className="eyebrow mb-1">best opportunity</p>
                                    <p className="font-mono text-sm text-brass">
                                        {funnel.best_opportunity?.toFixed(1) ?? '—'}
                                    </p>
                                </div>
                            </div>
                        </div>
                    </div>
                </>
            )}

            {diagnostic && (
                <div className="p-3 bg-warn/10 rounded-input flex items-start gap-2">
                    <AlertTriangle size={14} className="text-warn mt-0.5 shrink-0" />
                    <div>
                        <p className="text-xs text-warn font-medium">
                            Why nothing was selected
                        </p>
                        <p className="text-xs text-ink2 mt-0.5 leading-relaxed">
                            {diagnostic.message}
                        </p>
                    </div>
                </div>
            )}
        </section>
    );
}
