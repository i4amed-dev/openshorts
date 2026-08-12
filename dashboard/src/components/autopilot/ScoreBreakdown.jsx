import React from 'react';

/**
 * Why this source was chosen, component by component.
 *
 * The score alone ("74.3") tells an operator nothing they can act on. Showing
 * the weighted contributions makes the ranking auditable: if a bad pick keeps
 * winning on `views`, the fix is to lower that weight, and this is where you
 * can see that is what happened.
 */
const LABELS = {
    velocity: 'view velocity',
    views: 'total views',
    engagement: 'engagement rate',
    comments: 'comment activity',
    recency: 'freshness',
    chart_rank: 'chart position',
    relevance: 'topic match',
    duration_fit: 'length fit',
};

export default function ScoreBreakdown({ breakdown, compact = false }) {
    const contributions = breakdown?.contributions;
    if (!contributions) return null;

    const rows = Object.entries(contributions)
        .filter(([, value]) => Math.abs(value) > 0.001)
        .sort((a, b) => b[1] - a[1]);
    const penalties = Object.entries(breakdown.penalties || {})
        .filter(([, value]) => value > 0.001);
    const max = Math.max(...rows.map(([, v]) => v), 0.0001);
    const signals = breakdown.signals || {};

    return (
        <div className={compact ? 'space-y-1.5' : 'space-y-2'}>
            {rows.map(([key, value]) => (
                <div key={key} className="flex items-center gap-2">
                    <span className="text-xs text-muted lowercase w-28 shrink-0 truncate">
                        {LABELS[key] || key}
                    </span>
                    <div className="flex-1 h-1 bg-paper3 rounded-full overflow-hidden">
                        <div
                            className="h-full bg-brass rounded-full"
                            style={{ width: `${Math.min(100, (value / max) * 100)}%` }}
                        />
                    </div>
                    <span className="readout w-10 text-right">+{value.toFixed(1)}</span>
                </div>
            ))}

            {penalties.map(([key, value]) => (
                <div key={key} className="flex items-center gap-2">
                    <span className="text-xs text-warn lowercase w-28 shrink-0 truncate">
                        {key === 'channel_repeat' ? 'channel repeat' : 'seen before'}
                    </span>
                    <div className="flex-1 h-1 bg-paper3 rounded-full overflow-hidden">
                        <div
                            className="h-full bg-warn rounded-full"
                            style={{ width: `${Math.min(100, (value / max) * 100)}%` }}
                        />
                    </div>
                    <span className="readout w-10 text-right text-warn">−{value.toFixed(1)}</span>
                </div>
            ))}

            {!compact && (
                <div className="pt-2 mt-1 border-t border-rule flex flex-wrap gap-x-4 gap-y-1">
                    <span className="readout">{signals.views_per_hour} views/hr</span>
                    {signals.engagement_rate !== undefined && (
                        <span className="readout">
                            {(signals.engagement_rate * 100).toFixed(2)}% engaged
                        </span>
                    )}
                    {signals.age_hours !== null && signals.age_hours !== undefined && (
                        <span className="readout">{Math.round(signals.age_hours)}h old</span>
                    )}
                    {signals.chart_rank && <span className="readout">chart #{signals.chart_rank}</span>}
                </div>
            )}
        </div>
    );
}
