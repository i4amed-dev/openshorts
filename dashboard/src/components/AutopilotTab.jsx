import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, Loader2 } from 'lucide-react';
import AutopilotOps from './autopilot/AutopilotOps';
import AutopilotSetup from './autopilot/AutopilotSetup';
import { apiFetch, apiJson } from '../lib/api';

/**
 * The Autopilot tab: operations by default, setup when asked for.
 *
 * Polls status on a timer because Autopilot keeps working with no browser open —
 * the page is a window onto a process that already ran, not a driver of it. The
 * poll pauses when the tab is hidden so an idle laptop tab is not making a
 * request every eight seconds all night.
 */
const POLL_MS = 8000;

const ACTIONS = {
    enable: () => ['/api/autopilot/enable', {}],
    disable: () => ['/api/autopilot/disable', {}],
    pause: () => ['/api/autopilot/pause', {}],
    resume: () => ['/api/autopilot/resume', {}],
    discover: () => ['/api/autopilot/discover', {}],
    'process-next': () => ['/api/autopilot/process-next', {}],
    'emergency-stop': () => ['/api/autopilot/emergency-stop', {}],
    'skip-source': (id) => [`/api/autopilot/sources/${id}/skip`, {}],
    'retry-source': (id) => [`/api/autopilot/sources/${id}/retry`, {}],
    'retry-publish': (id) => [`/api/autopilot/publishes/${id}/retry`, {}],
    'force-retry-publish': (id) => [`/api/autopilot/publishes/${id}/force-retry`, {}],
    'resolve-publish': (id) => [`/api/autopilot/publishes/${id}/resolve`, {}],
};

export default function AutopilotTab() {
    const [view, setView] = useState('ops');       // ops | setup
    const [status, setStatus] = useState(null);
    const [settings, setSettings] = useState(null);
    const [busy, setBusy] = useState(false);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [loadError, setLoadError] = useState('');
    const timer = useRef(null);

    const refresh = useCallback(async () => {
        try {
            const data = await apiJson('/api/autopilot/status');
            setStatus(data);
            setLoadError('');
        } catch (e) {
            setLoadError(e.detail || 'Could not reach the Autopilot service.');
        }
    }, []);

    const loadSettings = useCallback(async () => {
        try {
            setSettings(await apiJson('/api/autopilot/settings'));
        } catch (e) {
            setLoadError(e.detail || 'Could not load Autopilot settings.');
        }
    }, []);

    useEffect(() => {
        refresh();
        loadSettings();
    }, [refresh, loadSettings]);

    useEffect(() => {
        const tick = () => { if (!document.hidden) refresh(); };
        timer.current = setInterval(tick, POLL_MS);
        document.addEventListener('visibilitychange', tick);
        return () => {
            clearInterval(timer.current);
            document.removeEventListener('visibilitychange', tick);
        };
    }, [refresh]);

    const action = async (name, args) => {
        const build = ACTIONS[name];
        if (!build) return;
        const [path] = build(args);
        setBusy(true);
        setError('');
        try {
            const res = await apiFetch(path, { method: 'POST' });
            if (!res.ok) {
                const body = await res.text();
                let detail = body;
                try { detail = JSON.parse(body).detail || body; } catch { /* raw text */ }
                setError(typeof detail === 'string' ? detail : 'That action was refused.');
            }
        } catch (e) {
            setError(e.message || 'That action failed.');
        } finally {
            setBusy(false);
            await refresh();
            if (name === 'enable' || name === 'disable') await loadSettings();
        }
    };

    const saveSettings = async (draft) => {
        setSaving(true);
        setError('');
        try {
            const saved = await apiJson('/api/autopilot/settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(draft),
            });
            setSettings(saved);
            await refresh();
            return true;
        } catch (e) {
            setError(e.detail || 'Those settings were rejected.');
            return false;
        } finally {
            setSaving(false);
        }
    };

    return (
        <div className="h-full overflow-y-auto p-4 sm:p-8 max-w-4xl mx-auto animate-fade">
            <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4 mb-6">
                <div>
                    <p className="eyebrow mb-1.5">06 · AUTOPILOT</p>
                    <h1 className="font-display lowercase text-2xl text-ink">
                        {view === 'setup' ? 'autopilot setup' : 'autopilot'}
                    </h1>
                    <p className="text-xs text-muted mt-1.5 max-w-lg leading-relaxed">
                        Finds sources on YouTube, runs them through the Clip Generator and
                        schedules the clips — with no browser open.
                    </p>
                </div>
                {view === 'setup' && (
                    <button onClick={() => setView('ops')} className="btn-quiet px-4 py-2 text-xs">
                        back
                    </button>
                )}
            </div>

            {loadError && (
                <div className="card p-4 mb-5 flex items-start gap-2 border-danger/40">
                    <AlertTriangle size={15} className="text-danger mt-0.5 shrink-0" />
                    <div>
                        <p className="text-sm text-danger">{loadError}</p>
                        <p className="text-xs text-muted mt-1">
                            Autopilot runs in the backend. Check that the container is up and
                            that <code>AUTOPILOT_ENABLED</code> is not set to 0.
                        </p>
                    </div>
                </div>
            )}

            {error && view === 'ops' && (
                <div className="card p-3 mb-5 flex items-center gap-2 border-warn/40">
                    <AlertTriangle size={14} className="text-warn shrink-0" />
                    <p className="text-xs text-warn">{error}</p>
                </div>
            )}

            {busy && (
                <div className="flex items-center gap-2 text-xs text-muted mb-3">
                    <Loader2 size={12} className="animate-spin" /> working…
                </div>
            )}

            {view === 'ops' ? (
                <AutopilotOps
                    status={status}
                    busy={busy}
                    action={action}
                    onOpenSetup={() => { setView('setup'); loadSettings(); }}
                />
            ) : (
                <AutopilotSetup
                    settings={settings}
                    onSave={saveSettings}
                    saving={saving}
                    error={error}
                    onDone={() => setView('ops')}
                />
            )}
        </div>
    );
}
