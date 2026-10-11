import { useEffect, useState } from 'react';
import { approvals, reports, scans } from '../../lib/api';
import { ROLE_LABEL, useAuth } from '../../lib/auth';
import type { ScanItem } from '../../lib/contract';
import { Spinner, StatusBadge, fmtDate, go } from './shared';

type Summary = Awaited<ReturnType<typeof reports.summary>>;
type Low = Awaited<ReturnType<typeof reports.lowStock>>;

function Spark({ values }: { values: number[] }) {
  const max = Math.max(1, ...values);
  const pts = values.map((v, i) => `${(i / Math.max(values.length - 1, 1)) * 100},${28 - (v / max) * 26}`).join(' ');
  return <svg viewBox="0 0 100 30" className="h-10 w-full" role="img" aria-label={`Shrinkage per day, last ${values.length} days: ${values.join(', ')}`}><polyline points={pts} fill="none" stroke="#0c7a45" strokeWidth="2" /></svg>;
}

function Tile({ label, value, onClick }: { label: string; value: string | number; onClick?: () => void }) {
  const inner = <><p className="text-[10px] font-bold uppercase tracking-wider text-muted">{label}</p><p className="mt-1 font-mono text-3xl font-bold tabular-nums">{value}</p></>;
  return onClick ? <button type="button" onClick={onClick} className="rounded-2xl border border-line bg-white p-4 text-left shadow-card hover:border-brand/40">{inner}</button> : <div className="rounded-2xl border border-line bg-white p-4 shadow-card">{inner}</div>;
}

export function HomePage() {
  const { user, can } = useAuth();
  const [inbox, setInbox] = useState<ScanItem[] | null>(null);
  const [recent, setRecent] = useState<ScanItem[] | null>(null);
  const [sum, setSum] = useState<Summary | null>(null);
  const [low, setLow] = useState<Low>([]);

  useEffect(() => {
    if (can('scan.approve')) approvals.inbox().then(setInbox).catch(() => setInbox([]));
    scans.list({ limit: 6 }).then(setRecent).catch(() => setRecent([]));
    reports.summary().then(setSum).catch(() => undefined);
    reports.lowStock().then(setLow).catch(() => undefined);
  }, [can]);

  if (!user) return null;
  return (
    <div className="space-y-5">
      <div><h1 className="text-2xl font-bold tracking-tight">Welcome, {user.name.split(' ')[0]}</h1><p className="text-sm text-muted">{ROLE_LABEL[user.role]} home</p></div>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {inbox && <Tile label="Waiting for approval" value={inbox.length} onClick={() => go('approvals')} />}
        {sum && <Tile label="Units short (latest counts)" value={sum.total_shrinkage} />}
        {sum && <Tile label="Auto-resolved" value={`${sum.accuracy}%`} />}
        <Tile label="Low-stock products" value={low.length} />
      </div>
      {sum && can('audit.view') && <div className="rounded-2xl border border-line bg-white p-4"><p className="text-xs font-bold">Shrinkage, last 8 days</p><Spark values={sum.shrinkage_trend} /></div>}
      {low.length > 0 && (
        <div className="rounded-2xl border border-line bg-white p-4">
          <p className="mb-2 text-xs font-bold">Low stock</p>
          <ul className="divide-y divide-line text-sm">{low.slice(0, 6).map((l) => <li key={l.sku} className="flex justify-between py-2"><span>{l.name}</span><span className="font-mono text-xs text-muted">{l.on_hand} / min {l.min_qty} · reorder {l.reorder}</span></li>)}</ul>
        </div>
      )}
      <div>
        <p className="mb-2 text-xs font-bold">Recent scans</p>
        {!recent ? <Spinner /> : recent.length === 0 ? <p className="text-sm text-muted">No scans yet.</p> : (
          <ul className="grid gap-2 md:grid-cols-2">{recent.map((s) => <li key={s.scan_id}><button type="button" onClick={() => go(`scans/${s.scan_id}`)} className="flex w-full items-center justify-between rounded-xl border border-line bg-white px-4 py-3 text-left text-sm hover:border-brand/40"><span><b>{s.shelf_name}</b><span className="block text-xs text-muted">{fmtDate(s.created_at)}</span></span><StatusBadge status={s.status} /></button></li>)}</ul>
        )}
      </div>
    </div>
  );
}
