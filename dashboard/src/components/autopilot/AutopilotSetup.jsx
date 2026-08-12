import React, { useCallback, useEffect, useState } from 'react';
import {
    AlertTriangle, Check, ChevronDown, ChevronRight, Link2, Loader2, RefreshCw,
    Save, Shield, X,
} from 'lucide-react';
import SegmentedControl from '../ui/SegmentedControl';
import { apiJson } from '../../lib/api';

/**
 * Autopilot configuration.
 *
 * Basic covers what almost everyone changes: where to look, what you are allowed
 * to use, and when to post. Advanced holds the filters and ranking weights — not
 * hidden, just out of the way, because forty fields on one screen is how people
 * click "save" without reading the one that matters (the rights policy).
 */

const TIMEZONES = [
    'UTC', 'Europe/London', 'Europe/Madrid', 'Europe/Paris', 'Europe/Berlin',
    'Europe/Lisbon', 'Europe/Rome', 'Europe/Istanbul', 'America/New_York',
    'America/Chicago', 'America/Denver', 'America/Los_Angeles', 'America/Sao_Paulo',
    'America/Mexico_City', 'Africa/Lagos', 'Asia/Dubai', 'Asia/Kolkata',
    'Asia/Singapore', 'Asia/Tokyo', 'Australia/Sydney', 'Pacific/Auckland',
];

const REGIONS = [
    ['US', 'United States'], ['GB', 'United Kingdom'], ['CA', 'Canada'],
    ['AU', 'Australia'], ['DE', 'Germany'], ['FR', 'France'], ['ES', 'Spain'],
    ['IT', 'Italy'], ['PT', 'Portugal'], ['NL', 'Netherlands'], ['BR', 'Brazil'],
    ['MX', 'Mexico'], ['IN', 'India'], ['JP', 'Japan'], ['KR', 'South Korea'],
    ['NG', 'Nigeria'], ['ZA', 'South Africa'],
];

// YouTube's assignable video categories (videoCategories.list, US).
const CATEGORIES = [
    ['1', 'Film & Animation'], ['2', 'Autos & Vehicles'], ['10', 'Music'],
    ['15', 'Pets & Animals'], ['17', 'Sports'], ['20', 'Gaming'],
    ['22', 'People & Blogs'], ['23', 'Comedy'], ['24', 'Entertainment'],
    ['25', 'News & Politics'], ['26', 'Howto & Style'], ['27', 'Education'],
    ['28', 'Science & Technology'],
];

const RIGHTS_POLICIES = [
    {
        value: 'CREATIVE_COMMONS_ONLY',
        label: 'Creative Commons only',
        blurb: 'Only sources the uploader licensed for reuse (CC-BY). The safe default '
            + 'for automatic discovery of other people’s videos.',
    },
    {
        value: 'OWNED_OR_ALLOWLISTED_CHANNELS',
        label: 'My own / approved channels',
        blurb: 'Only channels you list below — normally your own, or ones that gave you '
            + 'permission in writing. Licence is ignored.',
    },
    {
        value: 'CREATIVE_COMMONS_OR_ALLOWLISTED',
        label: 'Creative Commons or approved channels',
        blurb: 'Either of the above. Use it only if you understand the rights position '
            + 'for every channel you add.',
    },
];

function Field({ label, hint, children }) {
    return (
        <div className="mb-4">
            <label className="eyebrow block mb-2">{label}</label>
            {children}
            {hint && <p className="text-xs text-muted mt-1.5 leading-relaxed">{hint}</p>}
        </div>
    );
}

function TimeList({ value, onChange, disabled }) {
    const times = value || [];
    const setAt = (index, next) => {
        const copy = [...times];
        copy[index] = next;
        onChange(copy);
    };
    return (
        <div className="flex flex-wrap gap-2">
            {times.map((time, index) => (
                <div key={index} className="flex items-center gap-1">
                    <input
                        type="time"
                        value={time}
                        disabled={disabled}
                        onChange={(e) => setAt(index, e.target.value)}
                        className="input-field w-auto py-2 [color-scheme:dark]"
                    />
                    {times.length > 1 && (
                        <button
                            type="button"
                            onClick={() => onChange(times.filter((_, i) => i !== index))}
                            className="text-muted hover:text-danger px-1"
                            aria-label="remove time"
                        >
                            ×
                        </button>
                    )}
                </div>
            ))}
            <button
                type="button"
                disabled={disabled || times.length >= 12}
                onClick={() => onChange([...times, '12:00'])}
                className="btn-quiet px-3 py-2 text-xs"
            >
                + add time
            </button>
        </div>
    );
}

function ListInput({ value, onChange, placeholder, disabled }) {
    return (
        <textarea
            rows={3}
            disabled={disabled}
            value={(value || []).join('\n')}
            placeholder={placeholder}
            onChange={(e) => onChange(
                e.target.value.split('\n').map((s) => s.trim()).filter(Boolean))}
            className="input-field font-mono text-xs resize-y"
        />
    );
}

function Num({ value, onChange, min, max, step = 1, disabled, suffix }) {
    return (
        <div className="flex items-center gap-2">
            <input
                type="number"
                value={value ?? ''}
                min={min}
                max={max}
                step={step}
                disabled={disabled}
                onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
                className="input-field py-2"
            />
            {suffix && <span className="readout shrink-0">{suffix}</span>}
        </div>
    );
}

const DAYS = [['0', 'Mon'], ['1', 'Tue'], ['2', 'Wed'], ['3', 'Thu'], ['4', 'Fri'],
    ['5', 'Sat'], ['6', 'Sun']];

export default function AutopilotSetup({ settings, onSave, saving, error, onDone }) {
    const [draft, setDraft] = useState(settings);
    const [advanced, setAdvanced] = useState(false);
    const [savedAt, setSavedAt] = useState(null);
    const [profiles, setProfiles] = useState(null);
    const [profileError, setProfileError] = useState('');

    useEffect(() => { setDraft(settings); }, [settings]);

    const loadProfiles = useCallback(async () => {
        setProfileError('');
        try {
            const data = await apiJson('/api/autopilot/profiles');
            setProfiles(data.profiles || []);
        } catch (e) {
            setProfiles([]);
            setProfileError(e.detail
                || 'Could not reach Upload-Post. Check UPLOAD_POST_API_KEY in the backend .env.');
        }
    }, []);

    useEffect(() => { loadProfiles(); }, [loadProfiles]);

    if (!draft) {
        return (
            <div className="flex items-center gap-2 text-muted text-sm py-12 justify-center">
                <Loader2 size={16} className="animate-spin" /> loading settings…
            </div>
        );
    }

    const set = (path, value) => {
        setDraft((prev) => {
            const next = structuredClone(prev);
            const keys = path.split('.');
            let node = next;
            for (const key of keys.slice(0, -1)) node = node[key];
            node[keys[keys.length - 1]] = value;
            return next;
        });
        setSavedAt(null);
    };

    const handleSave = async () => {
        const ok = await onSave(draft);
        if (ok) setSavedAt(Date.now());
    };

    const needsChannels = draft.rights.policy !== 'CREATIVE_COMMONS_ONLY';
    const usesNiche = draft.discovery.strategies.includes('niche_search');
    const selectedProfile = (profiles || []).find(
        (p) => p.username === draft.publishing.upload_post_user);
    const connected = selectedProfile ? (selectedProfile.connected || []) : null;
    const unavailable = connected
        ? draft.publishing.platforms.filter((p) => !connected.includes(p))
        : [];

    return (
        <div className="space-y-5">
            {error && (
                <div className="card p-4 flex items-start gap-2 border-danger/40">
                    <AlertTriangle size={15} className="text-danger mt-0.5 shrink-0" />
                    <p className="text-sm text-danger">{error}</p>
                </div>
            )}

            {/* --- Rights: first, because it is the decision that matters most --- */}
            <section className="card p-5">
                <div className="flex items-center gap-2 mb-1">
                    <Shield size={15} className="text-brass" />
                    <h3 className="text-sm text-ink lowercase">source rights policy</h3>
                </div>
                <p className="text-xs text-muted mb-4 leading-relaxed">
                    In manual mode you tick a box confirming you have the rights, one video at a
                    time. Autopilot has nobody at the keyboard, so it carries this policy instead
                    and records it against every source it processes.
                </p>

                <div className="space-y-2">
                    {RIGHTS_POLICIES.map((option) => (
                        <button
                            key={option.value}
                            type="button"
                            onClick={() => set('rights.policy', option.value)}
                            className={`w-full text-left p-3 rounded-input border transition-colors ${
                                draft.rights.policy === option.value
                                    ? 'border-brass bg-paper3'
                                    : 'border-rule hover:border-rule2'}`}
                        >
                            <div className="flex items-center gap-2">
                                <span className={`text-sm ${
                                    draft.rights.policy === option.value ? 'text-ink' : 'text-ink2'}`}>
                                    {option.label}
                                </span>
                                {draft.rights.policy === option.value && (
                                    <Check size={13} className="text-brass" />
                                )}
                            </div>
                            <p className="text-xs text-muted mt-1 leading-relaxed">{option.blurb}</p>
                        </button>
                    ))}
                </div>

                {needsChannels && (
                    <div className="mt-4">
                        <Field
                            label="approved channel ids"
                            hint="One YouTube channel id per line (starts with UC, 24 characters).
                                  Find it in the channel's About → Share → Copy channel ID."
                        >
                            <ListInput
                                value={draft.rights.allowlisted_channel_ids}
                                onChange={(v) => set('rights.allowlisted_channel_ids', v)}
                                placeholder="UCxxxxxxxxxxxxxxxxxxxxxx"
                            />
                        </Field>
                    </div>
                )}
            </section>

            {/* --- Discovery ---------------------------------------------------- */}
            <section className="card p-5">
                <h3 className="text-sm text-ink lowercase mb-4">what to look for</h3>

                <Field label="strategy">
                    <SegmentedControl
                        multi
                        options={[
                            { value: 'most_popular', label: 'trending', hint: '1 quota unit' },
                            { value: 'niche_search', label: 'my topics', hint: '100 units each' },
                        ]}
                        value={draft.discovery.strategies}
                        onChange={(v) => set('discovery.strategies',
                            v.length ? v : ['most_popular'])}
                    />
                </Field>

                {usesNiche && (
                    <Field
                        label="topics / keywords"
                        hint="One per line. Each topic costs 100 YouTube quota units per discovery
                              run, out of a 10,000/day default — keep the list short."
                    >
                        <ListInput
                            value={draft.discovery.topics}
                            onChange={(v) => set('discovery.topics', v)}
                            placeholder={'chess endgames\nspace launch'}
                        />
                    </Field>
                )}

                <div className="grid sm:grid-cols-2 gap-x-4">
                    <Field label="region">
                        <select
                            value={draft.discovery.region_code}
                            onChange={(e) => set('discovery.region_code', e.target.value)}
                            className="input-field appearance-none cursor-pointer py-2"
                        >
                            {REGIONS.map(([code, name]) => (
                                <option key={code} value={code}>{name}</option>
                            ))}
                        </select>
                    </Field>
                    <Field label="language">
                        <input
                            value={draft.discovery.relevance_language}
                            onChange={(e) => set('discovery.relevance_language', e.target.value)}
                            className="input-field py-2"
                            placeholder="en"
                        />
                    </Field>
                </div>

                <Field label="categories" hint="Leave all unselected for every category.">
                    <div className="flex flex-wrap gap-1.5">
                        {CATEGORIES.map(([id, name]) => {
                            const on = draft.discovery.category_ids.includes(id);
                            return (
                                <button
                                    key={id}
                                    type="button"
                                    onClick={() => set('discovery.category_ids',
                                        on ? draft.discovery.category_ids.filter((c) => c !== id)
                                           : [...draft.discovery.category_ids, id])}
                                    className={`px-2.5 py-1.5 rounded-input border text-xs lowercase transition-colors ${
                                        on ? 'border-brass bg-paper3 text-ink'
                                           : 'border-rule text-muted hover:text-ink2'}`}
                                >
                                    {name}
                                </button>
                            );
                        })}
                    </div>
                </Field>
            </section>

            {/* --- Schedule ----------------------------------------------------- */}
            <section className="card p-5">
                <h3 className="text-sm text-ink lowercase mb-4">when</h3>

                <Field
                    label="timezone"
                    hint="Every time below is wall-clock time in this zone. Klippo stores UTC
                          internally, so daylight saving never shifts your schedule."
                >
                    <select
                        value={draft.timezone}
                        onChange={(e) => set('timezone', e.target.value)}
                        className="input-field appearance-none cursor-pointer py-2"
                    >
                        {TIMEZONES.map((zone) => <option key={zone} value={zone}>{zone}</option>)}
                    </select>
                </Field>

                <Field
                    label="discovery times"
                    hint="When Klippo searches YouTube and starts generating. Overnight keeps the
                          machine free during the day."
                >
                    <TimeList
                        value={draft.schedule.discovery_times}
                        onChange={(v) => set('schedule.discovery_times', v)}
                    />
                </Field>

                <Field label="publishing slots" hint="When finished clips go out.">
                    <TimeList
                        value={draft.schedule.publish_times}
                        onChange={(v) => set('schedule.publish_times', v)}
                    />
                </Field>

                <Field label="active days">
                    <div className="flex flex-wrap gap-1.5">
                        {DAYS.map(([value, label]) => {
                            const day = Number(value);
                            const on = draft.schedule.days_of_week.includes(day);
                            return (
                                <button
                                    key={value}
                                    type="button"
                                    onClick={() => set('schedule.days_of_week',
                                        on ? draft.schedule.days_of_week.filter((d) => d !== day)
                                           : [...draft.schedule.days_of_week, day].sort())}
                                    className={`px-3 py-1.5 rounded-input border text-xs lowercase transition-colors ${
                                        on ? 'border-brass bg-paper3 text-ink'
                                           : 'border-rule text-muted hover:text-ink2'}`}
                                >
                                    {label}
                                </button>
                            );
                        })}
                    </div>
                </Field>

                <div className="grid sm:grid-cols-3 gap-x-4">
                    <Field label="max sources / day">
                        <Num value={draft.schedule.max_sources_per_day} min={1} max={10}
                            onChange={(v) => set('schedule.max_sources_per_day', v)} />
                    </Field>
                    <Field label="max posts / day">
                        <Num value={draft.schedule.max_posts_per_day} min={1} max={12}
                            onChange={(v) => set('schedule.max_posts_per_day', v)} />
                    </Field>
                    <Field label="min gap between posts">
                        <Num value={draft.schedule.min_spacing_minutes} min={0} max={1440}
                            suffix="min"
                            onChange={(v) => set('schedule.min_spacing_minutes', v)} />
                    </Field>
                </div>

                <Field
                    label="after downtime"
                    hint="What to do with a slot that passed while the Mac was asleep."
                >
                    <SegmentedControl
                        options={[
                            { value: 'next_slot', label: 'next free slot' },
                            { value: 'skip', label: 'skip it' },
                            { value: 'immediate', label: 'post now (spaced)' },
                        ]}
                        value={draft.schedule.catch_up_policy}
                        onChange={(v) => set('schedule.catch_up_policy', v)}
                    />
                </Field>
            </section>

            {/* --- Clips + platforms -------------------------------------------- */}
            <section className="card p-5">
                <h3 className="text-sm text-ink lowercase mb-4">what to publish</h3>

                {/* Upload-Post owns the social connections. Klippo never asks
                    for YouTube/Instagram/TikTok credentials — it only needs to
                    know which profile to post as, and what that profile can
                    actually reach. */}
                <Field
                    label="upload-post profile"
                    hint="Your social accounts are connected inside Upload-Post. Klippo just
                          needs to know which profile to publish as."
                >
                    {profileError ? (
                        <div className="p-3 bg-warn/10 rounded-input flex items-start gap-2">
                            <AlertTriangle size={14} className="text-warn mt-0.5 shrink-0" />
                            <div className="min-w-0">
                                <p className="text-xs text-warn">{profileError}</p>
                                <button type="button" onClick={loadProfiles}
                                    className="btn-quiet px-3 py-1.5 text-xs mt-2">
                                    <RefreshCw size={12} /> try again
                                </button>
                            </div>
                        </div>
                    ) : profiles === null ? (
                        <div className="flex items-center gap-2 text-xs text-muted">
                            <Loader2 size={13} className="animate-spin" /> loading profiles…
                        </div>
                    ) : profiles.length === 0 ? (
                        <p className="text-xs text-muted">
                            No profiles found on this Upload-Post key. Create one at
                            app.upload-post.com and connect your accounts there.
                        </p>
                    ) : (
                        <div className="space-y-2">
                            {profiles.map((profile) => {
                                const active = draft.publishing.upload_post_user === profile.username;
                                return (
                                    <button
                                        key={profile.username}
                                        type="button"
                                        onClick={() => set('publishing.upload_post_user',
                                            profile.username)}
                                        className={`w-full text-left p-3 rounded-input border transition-colors ${
                                            active ? 'border-brass bg-paper3'
                                                   : 'border-rule hover:border-rule2'}`}
                                    >
                                        <div className="flex items-center gap-2">
                                            <Link2 size={13} className={active ? 'text-brass' : 'text-muted'} />
                                            <span className={`text-sm ${active ? 'text-ink' : 'text-ink2'}`}>
                                                {profile.username}
                                            </span>
                                            {active && <Check size={13} className="text-brass" />}
                                        </div>
                                        <div className="flex flex-wrap gap-3 mt-1.5 ml-5">
                                            {['tiktok', 'instagram', 'youtube'].map((plat) => {
                                                const on = (profile.connected || []).includes(plat);
                                                return (
                                                    <span key={plat}
                                                        className={`readout flex items-center gap-1 ${
                                                            on ? 'text-ok' : 'text-muted opacity-60'}`}>
                                                        {on ? <Check size={10} /> : <X size={10} />}
                                                        {plat}
                                                    </span>
                                                );
                                            })}
                                        </div>
                                    </button>
                                );
                            })}
                        </div>
                    )}
                </Field>

                <Field
                    label="platforms"
                    hint={connected
                        ? 'Only platforms the selected profile has connected can be chosen.'
                        : 'Select a profile above to see which platforms are available.'}
                >
                    <SegmentedControl
                        multi
                        options={['tiktok', 'instagram', 'youtube'].map((value) => ({
                            value,
                            label: value,
                            // Disabled rather than hidden: the operator should see
                            // WHY a platform is unavailable, not wonder where it went.
                            disabled: !!connected && !connected.includes(value),
                            hint: connected && !connected.includes(value)
                                ? 'not connected' : undefined,
                        }))}
                        value={draft.publishing.platforms}
                        onChange={(v) => set('publishing.platforms',
                            v.length ? v : draft.publishing.platforms)}
                    />
                </Field>

                {unavailable.length > 0 && (
                    <div className="mb-4 p-3 bg-warn/10 rounded-input flex items-start gap-2">
                        <AlertTriangle size={14} className="text-warn mt-0.5 shrink-0" />
                        <p className="text-xs text-warn">
                            {unavailable.join(', ')} {unavailable.length === 1 ? 'is' : 'are'} selected
                            for publishing, but the profile
                            “{draft.publishing.upload_post_user}” has no such account connected.
                            Autopilot will refuse to start until this is resolved.
                        </p>
                    </div>
                )}

                <div className="grid sm:grid-cols-3 gap-x-4">
                    <Field label="clips per source">
                        <Num value={draft.clips.max_clips_per_source} min={1} max={15}
                            onChange={(v) => set('clips.max_clips_per_source', v)} />
                    </Field>
                    <Field label="min clip length">
                        <Num value={draft.clips.min_clip_seconds} min={1} max={600} suffix="s"
                            onChange={(v) => set('clips.min_clip_seconds', v)} />
                    </Field>
                    <Field label="max clip length">
                        <Num value={draft.clips.max_clip_seconds} min={2} max={600} suffix="s"
                            onChange={(v) => set('clips.max_clip_seconds', v)} />
                    </Field>
                </div>

                <Field label="output format">
                    <SegmentedControl
                        options={[
                            { value: 'vertical', label: 'vertical 9:16' },
                            { value: 'square', label: 'square 1:1' },
                            { value: 'horizontal', label: 'wide 16:9' },
                        ]}
                        value={draft.processing.output_format}
                        onChange={(v) => set('processing.output_format', v)}
                    />
                </Field>
            </section>

            {/* --- Advanced ------------------------------------------------------ */}
            <section className="card p-5">
                <button
                    type="button"
                    onClick={() => setAdvanced(!advanced)}
                    className="flex items-center gap-2 w-full text-left"
                >
                    {advanced ? <ChevronDown size={14} className="text-muted" />
                        : <ChevronRight size={14} className="text-muted" />}
                    <h3 className="text-sm text-ink lowercase">advanced</h3>
                    <span className="readout ml-auto">filters · ranking · limits</span>
                </button>

                {advanced && (
                    <div className="mt-5 space-y-6 animate-fade">
                        <div>
                            <p className="eyebrow mb-3">source filters</p>
                            <div className="grid sm:grid-cols-2 gap-x-4">
                                <Field label="min source length">
                                    <Num value={draft.eligibility.min_duration_seconds}
                                        min={30} max={36000} suffix="s"
                                        onChange={(v) => set('eligibility.min_duration_seconds', v)} />
                                </Field>
                                <Field label="max source length">
                                    <Num value={draft.eligibility.max_duration_seconds}
                                        min={60} max={36000} suffix="s"
                                        onChange={(v) => set('eligibility.max_duration_seconds', v)} />
                                </Field>
                                <Field label="max age">
                                    <Num value={draft.eligibility.max_age_hours}
                                        min={1} max={8760} suffix="h"
                                        onChange={(v) => set('eligibility.max_age_hours', v)} />
                                </Field>
                                <Field label="min views">
                                    <Num value={draft.eligibility.min_views} min={0}
                                        onChange={(v) => set('eligibility.min_views', v)} />
                                </Field>
                                <Field label="min views / hour">
                                    <Num value={draft.eligibility.min_view_velocity_per_hour}
                                        min={0} step={10}
                                        onChange={(v) => set(
                                            'eligibility.min_view_velocity_per_hour', v)} />
                                </Field>
                                <Field label="min engagement rate"
                                    hint="(likes + comments) ÷ views. 0.005 = 0.5%.">
                                    <Num value={draft.eligibility.min_engagement_rate}
                                        min={0} max={1} step={0.001}
                                        onChange={(v) => set('eligibility.min_engagement_rate', v)} />
                                </Field>
                                <Field label="channel cooldown"
                                    hint="Don't reuse the same channel within this window.">
                                    <Num value={draft.eligibility.channel_cooldown_hours}
                                        min={0} max={8760} suffix="h"
                                        onChange={(v) => set(
                                            'eligibility.channel_cooldown_hours', v)} />
                                </Field>
                                <Field label="minimum source quality">
                                    <Num value={draft.processing.min_source_height}
                                        min={0} max={4320} suffix="p"
                                        onChange={(v) => set('processing.min_source_height', v)} />
                                </Field>
                            </div>

                            <Field label="require captions">
                                <SegmentedControl
                                    options={[{ value: 'no', label: 'no' },
                                              { value: 'yes', label: 'yes' }]}
                                    value={draft.eligibility.require_captions ? 'yes' : 'no'}
                                    onChange={(v) => set('eligibility.require_captions', v === 'yes')}
                                />
                            </Field>

                            <div className="grid sm:grid-cols-2 gap-x-4">
                                <Field label="channel denylist">
                                    <ListInput value={draft.discovery.channel_denylist}
                                        onChange={(v) => set('discovery.channel_denylist', v)}
                                        placeholder="UCxxxx…" />
                                </Field>
                                <Field label="channel allowlist"
                                    hint="If set, only these channels are considered at all.">
                                    <ListInput value={draft.discovery.channel_allowlist}
                                        onChange={(v) => set('discovery.channel_allowlist', v)}
                                        placeholder="UCxxxx…" />
                                </Field>
                                <Field label="exclude keywords">
                                    <ListInput value={draft.eligibility.keywords_none}
                                        onChange={(v) => set('eligibility.keywords_none', v)}
                                        placeholder="giveaway" />
                                </Field>
                                <Field label="require any keyword">
                                    <ListInput value={draft.eligibility.keywords_any}
                                        onChange={(v) => set('eligibility.keywords_any', v)}
                                        placeholder="interview" />
                                </Field>
                            </div>
                        </div>

                        <div>
                            <p className="eyebrow mb-1">ranking weights</p>
                            <p className="text-xs text-muted mb-3 leading-relaxed">
                                Each signal is normalised across the candidate set before being
                                weighted, so raising one weight changes the ordering rather than
                                letting a big number dominate.
                            </p>
                            <div className="grid sm:grid-cols-2 gap-x-4">
                                {Object.keys(draft.ranking.weights).map((key) => (
                                    <Field key={key} label={key.replace(/_/g, ' ')}>
                                        <Num value={draft.ranking.weights[key]}
                                            min={0} max={10} step={0.05}
                                            onChange={(v) => set(`ranking.weights.${key}`, v)} />
                                    </Field>
                                ))}
                            </div>
                        </div>

                        <div>
                            <p className="eyebrow mb-3">safety limits</p>
                            <div className="grid sm:grid-cols-2 gap-x-4">
                                <Field label="processing retries per source">
                                    <Num value={draft.limits.max_process_attempts} min={1} max={5}
                                        onChange={(v) => set('limits.max_process_attempts', v)} />
                                </Field>
                                <Field label="publish retries per clip">
                                    <Num value={draft.limits.max_publish_attempts} min={1} max={10}
                                        onChange={(v) => set('limits.max_publish_attempts', v)} />
                                </Field>
                                <Field
                                    label="stop after N failures in a row"
                                    hint="The circuit breaker. Autopilot pauses itself and waits
                                          for you rather than burning API credits."
                                >
                                    <Num value={draft.limits.consecutive_failure_limit}
                                        min={1} max={50}
                                        onChange={(v) => set(
                                            'limits.consecutive_failure_limit', v)} />
                                </Field>
                                <Field label="youtube quota budget / day">
                                    <Num value={draft.limits.youtube_daily_quota_units}
                                        min={100} step={100}
                                        onChange={(v) => set(
                                            'limits.youtube_daily_quota_units', v)} />
                                </Field>
                            </div>
                        </div>
                    </div>
                )}
            </section>

            {/* --- Save --------------------------------------------------------- */}
            <div className="flex flex-wrap items-center gap-3">
                <button onClick={handleSave} disabled={saving} className="btn-primary px-6 py-3">
                    {saving ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}
                    save settings
                </button>
                {onDone && (
                    <button onClick={onDone} className="btn-ghost px-5 py-3">back to autopilot</button>
                )}
                {savedAt && (
                    <span className="badge-ok"><Check size={11} /> saved</span>
                )}
            </div>
        </div>
    );
}
