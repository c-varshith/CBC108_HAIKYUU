import { useEffect, useState } from 'react';
import { audit } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import { Notice, Spinner, errMsg, fmtDate, go } from './shared';

type Row = Awaited<ReturnType<typeof audit.list>>[number];
const show = (v: unknown) => (v == null ? '' : typeof v === 'string' ? v : JSON.stringify(v));

export function AuditPage() {
  const { can } = useAuth();
  const [q, setQ] = useState('');
  const [rows, setRows] = useState<Row[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const t = window.setTimeout(() => { audit.list({ q, limit: 200 }).then(setRows).catch((e) => setError(errMsg(e))); }, 250);
    return () => window.clearTimeout(t);
  }, [q]);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold tracking-tight">Audit log</h1>
        <div className="flex gap-2">
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search" aria-label="Search audit log" className="rounded-xl border border-line-strong bg-white px-3 py-2 text-sm" />
          {can('reports.export') && <button type="button" onClick={() => audit.export().catch((e) => setError(errMsg(e)))} className="rounded-full border border-line-strong bg-white px-4 py-2 text-sm font-semibold">Export CSV</button>}
        </div>
      </div>
      {error && <Notice>{error}</Notice>}
      {!rows && !error && <Spinner />}
      <div className="overflow-x-auto rounded-2xl border border-line bg-white">
        <table className="w-full text-left text-xs">
          <thead><tr className="text-[10px] uppercase tracking-wider text-muted"><th className="p-3">When</th><th className="p-3">Who</th><th className="p-3">Action</th><th className="p-3">Entity</th><th className="p-3">Change</th></tr></thead>
          <tbody>
            {rows?.map((r) => (
              <tr key={r.id} className="border-t border-line align-top">
                <td className="whitespace-nowrap p-3">{fmtDate(r.at)}</td><td className="p-3">{r.actor}</td>
                <td className="p-3 font-mono font-bold">{r.action}</td>
                <td className="p-3">{r.scan_id ? <button type="button" className="font-mono text-brand underline" onClick={() => go(`scans/${r.scan_id}`)}>{r.entity}</button> : r.entity}</td>
                <td className="p-3 font-mono text-muted">{show(r.before)}{r.before != null && r.after != null ? ' → ' : ''}{show(r.after)}</td>
              </tr>
            ))}
            {rows?.length === 0 && <tr><td colSpan={5} className="p-8 text-center text-muted">No entries.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
