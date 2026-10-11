import { useEffect, useState } from 'react';
import { approvals, scans } from '../../lib/api';
import type { ScanItem } from '../../lib/contract';
import { Notice, Spinner, errMsg, fmtDate, go } from './shared';

export function ApprovalsPage() {
  const [rows, setRows] = useState<ScanItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [previous, setPrevious] = useState<Record<string, ScanItem | undefined>>({});
  useEffect(() => {
    let alive = true;
    const load = () => approvals.inbox().then(async (r) => {
      if (!alive) return;
      setRows(r);
      const shelfIds = [...new Set(r.map((x) => x.shelf_id))];
      const pairs = await Promise.all(shelfIds.map(async (shelfId) => {
        const approved = await scans.list({ status: 'APPROVED', shelf_id: shelfId, limit: 2 });
        return [shelfId, approved[0]] as const;
      }));
      if (alive) setPrevious(Object.fromEntries(pairs));
    }).catch((e) => { if (alive) setError(errMsg(e)); });
    load();
    const t = window.setInterval(load, 10000);
    return () => { alive = false; window.clearInterval(t); };
  }, []);
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold tracking-tight">Approval inbox</h1>
      {error && <Notice>{error}</Notice>}
      {!rows && !error && <Spinner />}
      {rows?.length === 0 && <p className="rounded-2xl border border-dashed border-line-strong bg-white p-10 text-center text-sm text-muted">Nothing is waiting for approval.</p>}
      <ul className="grid gap-3 md:grid-cols-2">
        {rows?.map((s) => (
          <li key={s.scan_id}>
            <div className="w-full rounded-2xl border border-line bg-white p-4 text-left shadow-card hover:border-brand/40">
              <button type="button" onClick={() => go(`scans/${s.scan_id}`)} className="w-full text-left">
              <p className="text-sm font-bold">{s.shelf_name}</p>
              <p className="mt-1 text-xs text-muted">Submitted by {s.submitted_by?.name ?? s.created_by.name}{s.submitted_at ? ` · ${fmtDate(s.submitted_at)}` : ''}</p>
              {s.submit_note && <p className="mt-2 rounded-lg bg-cream px-3 py-2 text-xs">“{s.submit_note}”</p>}
              <p className="mt-3 font-mono text-xs tabular-nums">{s.totals.detected} items · {s.can_approve ? 'you can approve' : 'you cannot approve this one'}</p>
              </button>
              {previous[s.shelf_id] && <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-xl bg-cream px-3 py-2 text-xs">
                <span>Previous approved: <b className="font-mono">{previous[s.shelf_id]?.totals.detected}</b> items · {previous[s.shelf_id] && fmtDate(previous[s.shelf_id]!.created_at)}
                  <b className="ml-2 font-mono">{s.totals.detected - (previous[s.shelf_id]?.totals.detected ?? 0) > 0 ? '+' : ''}{s.totals.detected - (previous[s.shelf_id]?.totals.detected ?? 0)}</b> vs previous
                </span>
                <a href={`#/app/compare/${previous[s.shelf_id]!.scan_id}/${s.scan_id}`} className="font-semibold text-brand underline">Compare images and counts</a>
              </div>}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
