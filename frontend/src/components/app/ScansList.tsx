import { useEffect, useState } from 'react';
import { scans } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import type { ScanItem } from '../../lib/contract';
import { Notice, Spinner, StatusBadge, errMsg, fmtDate, go } from './shared';

const STATUSES = ['', 'NEEDS_REVIEW', 'IN_REVIEW', 'RETURNED', 'SUBMITTED', 'APPROVED', 'FAILED'];

export function ScansList() {
  const { user } = useAuth();
  const [status, setStatus] = useState('');
  const [rows, setRows] = useState<ScanItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => scans.list({ status, limit: 100 }).then((r) => { if (alive) setRows(r); }).catch((e) => { if (alive) setError(errMsg(e)); });
    load();
    // keep in-flight scans fresh (a scan started on another device shows up and progresses here)
    const t = window.setInterval(load, 8000);
    return () => { alive = false; window.clearInterval(t); };
  }, [status]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-2xl font-bold tracking-tight">{user?.role === 'STAFF' ? 'My scans' : 'Scans'}</h1>
        <label className="text-xs font-semibold">Status
          <select value={status} onChange={(e) => setStatus(e.target.value)} className="ml-2 rounded-lg border border-line-strong bg-white px-2.5 py-2 text-sm">
            {STATUSES.map((s) => <option key={s} value={s}>{s ? s.replace('_', ' ') : 'All'}</option>)}
          </select>
        </label>
      </div>
      {error && <Notice>{error}</Notice>}
      {!rows && !error && <Spinner />}
      {rows && rows.length === 0 && <p className="rounded-2xl border border-dashed border-line-strong bg-white p-10 text-center text-sm text-muted">No scans here yet.</p>}
      <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {rows?.map((s) => (
          <li key={s.scan_id}>
            <button type="button" onClick={() => go(`scans/${s.scan_id}`)} className="w-full rounded-2xl border border-line bg-white p-4 text-left shadow-card transition hover:border-brand/40">
              <div className="flex items-center justify-between gap-2"><p className="truncate text-sm font-bold">{s.shelf_name}</p><StatusBadge status={s.status} /></div>
              <p className="mt-1 text-xs text-muted">{s.created_by.name} · {fmtDate(s.created_at)}</p>
              <p className="mt-3 font-mono text-xs tabular-nums text-ink-2">{s.totals.detected} items · {s.totals.unresolved} to review</p>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
