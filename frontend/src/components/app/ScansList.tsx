import { useCallback, useEffect, useMemo, useState } from 'react';
import { CornerUpLeft, Trash2 } from 'lucide-react';
import { approvals, scans } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import type { ScanItem, ScanStatus } from '../../lib/contract';
import { Notice, Spinner, StatusBadge, errMsg, fmtDate, go } from './shared';

type View = 'all' | 'approvals' | 'active' | 'history';
const HISTORY_STATUSES: ScanStatus[] = ['APPROVED', 'RETURNED', 'FAILED', 'ARCHIVED'];
const ACTIVE_STATUSES: ScanStatus[] = ['DRAFT', 'UPLOADED', 'PROCESSING', 'NEEDS_REVIEW', 'IN_REVIEW', 'AUTO_READY', 'REOPENED'];

export function ScansList({ initialView = 'all' }: { initialView?: View }) {
  const { user, can } = useAuth();
  const canApprove = can('scan.approve');
  const [view, setView] = useState<View>(initialView);
  const [rows, setRows] = useState<ScanItem[] | null>(null);
  const [showCleared, setShowCleared] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [returning, setReturning] = useState<ScanItem | null>(null);
  const [returnNote, setReturnNote] = useState('');

  const load = useCallback(async () => {
    try {
      const [scansRows, approvalRows] = await Promise.all([
        scans.list({ include_cleared: showCleared, limit: 200 }),
        canApprove ? approvals.inbox() : Promise.resolve([]),
      ]);
      const approvalsById = new Map(approvalRows.map((s) => [s.scan_id, s]));
      const scanIds = new Set(scansRows.map((s) => s.scan_id));
      const merged = [
        ...scansRows.map((s) => ({ ...s, ...(approvalsById.get(s.scan_id) ?? {}) })),
        ...approvalRows.filter((s) => !scanIds.has(s.scan_id)),
      ].sort((a, b) => Date.parse(b.submitted_at ?? b.created_at) - Date.parse(a.submitted_at ?? a.created_at));
      setRows(merged);
      setError(null);
    } catch (e) {
      setError(errMsg(e));
    }
  }, [canApprove, showCleared]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 10000);
    return () => window.clearInterval(timer);
  }, [load]);

  const visible = useMemo(() => (rows ?? []).filter((s) => {
    if (!showCleared && s.cleared) return false;
    if (view === 'approvals') return s.status === 'SUBMITTED';
    if (view === 'active') return ACTIVE_STATUSES.includes(s.status);
    if (view === 'history') return HISTORY_STATUSES.includes(s.status);
    return true;
  }), [rows, showCleared, view]);
  async function clearHistory() {
    if (!window.confirm('Hide completed scans from your history? This only changes your view. Scans remain available to your team and continue to count in reports.')) return;
    setBusy(true);
    setError(null);
    try { await scans.clearHistory(); setShowCleared(false); await load(); }
    catch (e) { setError(errMsg(e)); }
    finally { setBusy(false); }
  }

  async function restore(id: string) {
    setBusy(true);
    setError(null);
    try { await scans.restoreHistory(id); await load(); }
    catch (e) { setError(errMsg(e)); }
    finally { setBusy(false); }
  }

  async function removeFromApprovals() {
    if (!returning || !returnNote.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await scans.return(returning.scan_id, returnNote.trim(), []);
      setReturning(null);
      setReturnNote('');
      await load();
    } catch (e) { setError(errMsg(e)); }
    finally { setBusy(false); }
  }

  async function deleteScan(scan: ScanItem) {
    const message = `Permanently delete this scan from ${scan.shelf_name}? Its detections, comments, and scan image will be deleted.`;
    if (!window.confirm(message)) return;
    setBusy(true);
    setError(null);
    try { await scans.remove(scan.scan_id); await load(); }
    catch (e) { setError(errMsg(e)); }
    finally { setBusy(false); }
  }

  const tabs: { key: View; label: string }[] = [
    { key: 'all', label: 'All scans' },
    ...(canApprove ? [{ key: 'approvals' as const, label: 'Awaiting approval' }] : []),
    { key: 'active', label: 'In progress' },
    { key: 'history', label: 'History' },
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">{user?.role === 'STAFF' ? 'My scans' : 'Scans & approvals'}</h1>
          <p className="mt-1 text-sm text-muted">Live scan records from your workspace.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {view === 'history' && <button type="button" disabled={busy} className="rounded-lg border border-line-strong bg-white px-3 py-2 text-sm font-semibold disabled:opacity-50" onClick={() => void clearHistory()}>Clear history</button>}
          {view === 'history' && <label className="flex items-center gap-2 text-xs font-semibold"><input type="checkbox" checked={showCleared} onChange={(e) => setShowCleared(e.target.checked)} />Show cleared</label>}
          <button type="button" className="rounded-lg border border-line-strong bg-white px-3 py-2 text-sm font-semibold" onClick={() => void load()}>Refresh</button>
        </div>
      </div>

      <div role="tablist" aria-label="Scan views" className="flex flex-wrap gap-2">
        {tabs.map((tab) => <button key={tab.key} type="button" role="tab" aria-selected={view === tab.key}
          onClick={() => setView(tab.key)} className={`rounded-full px-4 py-2 text-sm font-semibold ${view === tab.key ? 'bg-ink text-white' : 'border border-line bg-white text-muted hover:text-ink'}`}>
          {tab.label}{tab.key === 'approvals' && rows && <span className="ml-2 font-mono">{rows.filter((s) => s.status === 'SUBMITTED').length}</span>}
        </button>)}
      </div>

      {error && <Notice>{error}</Notice>}
      {!rows && !error && <Spinner />}
      {rows && visible.length === 0 && <p className="rounded-2xl border border-dashed border-line-strong bg-white p-10 text-center text-sm text-muted">
        {view === 'approvals' ? 'No scans are waiting for approval.' : view === 'history' ? 'No completed scans in history.' : 'No scans match this view.'}
      </p>}
      <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {visible.map((s) => (
          <li key={s.scan_id} className="rounded-2xl border border-line bg-white p-4 shadow-card">
            <button type="button" onClick={() => go(`scans/${s.scan_id}`)} className="w-full text-left">
              <div className="flex items-center justify-between gap-2"><p className="truncate text-sm font-bold">{s.shelf_name}</p><StatusBadge status={s.status} /></div>
              <p className="mt-1 text-xs text-muted">{s.status === 'SUBMITTED' ? `Submitted by ${s.submitted_by?.name ?? s.created_by.name} · ${fmtDate(s.submitted_at ?? s.created_at)}` : `${s.created_by.name} · ${fmtDate(s.created_at)}`}</p>
              <p className="mt-3 text-xs text-muted"><span className="font-mono tabular-nums">{s.totals.detected}</span> detected · <span className="font-mono tabular-nums">{s.totals.sure}</span> matched · <span className="font-mono tabular-nums">{s.totals.unknown}</span> unknown · <span className="font-mono tabular-nums">{s.totals.unresolved}</span> need review</p>
              {s.submit_note && <p className="mt-2 rounded-lg bg-cream px-3 py-2 text-xs">{s.submit_note}</p>}
              {s.status === 'SUBMITTED' && <p className="mt-2 text-xs font-semibold text-brand">{s.can_approve ? 'Ready for your approval' : 'Submitted for manager approval'}</p>}
            </button>
            {showCleared && s.cleared && <div className="mt-3 flex items-center justify-between gap-3 border-t border-line pt-3 text-xs text-muted"><span>Hidden from your history</span><button type="button" disabled={busy} className="font-semibold text-brand underline" onClick={() => void restore(s.scan_id)}>Restore</button></div>}
            {(canApprove && s.status === 'SUBMITTED' || can('scan.delete') && (user?.role === 'OWNER' || user?.role === 'STAFF' && s.created_by.id === user.id && s.status === 'DRAFT')) && <div className="mt-3 flex flex-wrap justify-end gap-2 border-t border-line pt-3">
              {canApprove && s.status === 'SUBMITTED' && <button type="button" disabled={busy} onClick={() => { setReturning(s); setReturnNote(''); }} className="inline-flex items-center gap-1.5 rounded-lg border border-warn/40 px-3 py-2 text-xs font-semibold text-warn hover:bg-warn-soft disabled:opacity-50"><CornerUpLeft className="h-3.5 w-3.5" />Remove from approvals</button>}
              {can('scan.delete') && (user?.role === 'OWNER' || user?.role === 'STAFF' && s.created_by.id === user.id && s.status === 'DRAFT') && <button type="button" disabled={busy} onClick={() => void deleteScan(s)} className="inline-flex items-center gap-1.5 rounded-lg border border-danger/30 px-3 py-2 text-xs font-semibold text-danger hover:bg-danger-soft disabled:opacity-50"><Trash2 className="h-3.5 w-3.5" />Delete</button>}
            </div>}
          </li>
        ))}
      </ul>
      {rows && <p className="text-center text-xs text-muted">Showing {visible.length} of up to {rows.length} recent scans.</p>}
      {returning && <div role="dialog" aria-modal="true" aria-labelledby="remove-approval-title" className="fixed inset-0 z-[70] flex items-end justify-center bg-ink/50 p-0 md:items-center md:p-4">
        <form onSubmit={(e) => { e.preventDefault(); void removeFromApprovals(); }} className="w-full max-w-md rounded-t-2xl bg-white p-5 shadow-pop md:rounded-2xl">
          <h2 id="remove-approval-title" className="text-lg font-bold">Remove from approvals?</h2>
          <p className="mt-2 text-sm text-muted">This returns “{returning.shelf_name}” to the submitter for correction. The scan and its history are kept.</p>
          <label className="mt-4 block text-xs font-semibold">Reason for returning
            <textarea required maxLength={2000} value={returnNote} onChange={(e) => setReturnNote(e.target.value)} rows={3} className="mt-1 w-full rounded-xl border border-line-strong p-3 text-sm font-normal" placeholder="Tell the submitter what to correct" />
          </label>
          <div className="mt-4 flex justify-end gap-2">
            <button type="button" disabled={busy} onClick={() => setReturning(null)} className="rounded-lg border border-line-strong px-4 py-2 text-sm font-semibold">Cancel</button>
            <button type="submit" disabled={busy || !returnNote.trim()} className="rounded-lg bg-warn px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{busy ? 'Returning…' : 'Return scan'}</button>
          </div>
        </form>
      </div>}
    </div>
  );
}
