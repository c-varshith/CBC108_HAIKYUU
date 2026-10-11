import { useEffect, useMemo, useState } from 'react';
import { scans } from '../../lib/api';
import type { ScanItem, ScanView } from '../../lib/contract';
import { Notice, ScanCanvas, Spinner, StatusBadge, errMsg, fmtDate } from './shared';

export function ScanCompare({ ids = [] }: { ids?: string[] }) {
  const requestedLeft = ids[0] ?? '';
  const requestedRight = ids[1] ?? '';
  const [items, setItems] = useState<ScanItem[] | null>(null);
  const [leftId, setLeftId] = useState('');
  const [rightId, setRightId] = useState('');
  const [left, setLeft] = useState<ScanView | null>(null);
  const [right, setRight] = useState<ScanView | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    scans.list({ limit: 100 }).then((rows) => {
      setItems(rows);
      setLeftId(requestedLeft && rows.some((x) => x.scan_id === requestedLeft) ? requestedLeft : rows[1]?.scan_id ?? rows[0]?.scan_id ?? '');
      setRightId(requestedRight && rows.some((x) => x.scan_id === requestedRight) ? requestedRight : rows[0]?.scan_id ?? '');
    }).catch((e) => setError(errMsg(e)));
  }, [requestedLeft, requestedRight]);
  useEffect(() => {
    let alive = true;
    if (leftId) scans.get(leftId).then((v) => { if (alive) setLeft(v); }).catch((e) => setError(errMsg(e)));
    else setLeft(null);
    return () => { alive = false; };
  }, [leftId]);
  useEffect(() => {
    let alive = true;
    if (rightId) scans.get(rightId).then((v) => { if (alive) setRight(v); }).catch((e) => setError(errMsg(e)));
    else setRight(null);
    return () => { alive = false; };
  }, [rightId]);

  const rows = useMemo(() => {
    const a = new Map((left?.summary ?? []).map((r) => [r.sku, r]));
    const b = new Map((right?.summary ?? []).map((r) => [r.sku, r]));
    return [...new Set([...a.keys(), ...b.keys()])].sort().map((sku) => ({
      sku, name: a.get(sku)?.name ?? b.get(sku)?.name ?? sku,
      before: a.get(sku)?.reviewed ?? 0, after: b.get(sku)?.reviewed ?? 0,
      delta: (b.get(sku)?.reviewed ?? 0) - (a.get(sku)?.reviewed ?? 0),
    }));
  }, [left, right]);

  if (!items && !error) return <Spinner />;
  if (!items?.length) return <div className="space-y-3"><h1 className="text-2xl font-bold">Compare scans</h1>{error ? <Notice>{error}</Notice> : <Notice tone="warn">Two scans are needed before you can compare shelf counts.</Notice>}</div>;
  const option = (s: ScanItem) => `${s.shelf_name} · ${fmtDate(s.created_at)} · ${s.scan_id.slice(0, 8)}`;
  return <div className="space-y-4">
    <div><h1 className="text-2xl font-bold tracking-tight">Compare scans</h1><p className="mt-1 text-sm text-muted">Review shelf images and item count changes side by side.</p></div>
    {error && <Notice>{error}</Notice>}
    <div className="grid gap-3 md:grid-cols-2">
      <label className="text-xs font-semibold">Earlier scan<select value={leftId} onChange={(e) => setLeftId(e.target.value)} className="mt-1 w-full rounded-xl border border-line-strong bg-white px-3 py-2.5 text-sm">{items.map((s) => <option key={s.scan_id} value={s.scan_id}>{option(s)}</option>)}</select></label>
      <label className="text-xs font-semibold">Later scan<select value={rightId} onChange={(e) => setRightId(e.target.value)} className="mt-1 w-full rounded-xl border border-line-strong bg-white px-3 py-2.5 text-sm">{items.map((s) => <option key={s.scan_id} value={s.scan_id}>{option(s)}</option>)}</select></label>
    </div>
    <div className="grid gap-4 xl:grid-cols-2">
      {[left, right].map((v, i) => <section key={i} className="min-w-0 space-y-2 rounded-2xl border border-line bg-white p-3">
        {!v ? <Spinner /> : <><div className="flex items-center justify-between gap-2"><span className="text-xs font-bold">{i === 0 ? 'Earlier' : 'Later'}</span><StatusBadge status={v.status} /><span className="font-mono text-xs">{v.totals.detected} detections</span></div>
          <ScanCanvas src={v.image.display_url} width={v.image.width} height={v.image.height} detections={v.detections} selectedId={null} filter="ALL" showLabels={false} onSelect={() => undefined} />
          <p className="text-xs text-muted">{v.created_by.name} · {v.scan_id.slice(0, 8)}</p></>}
      </section>)}
    </div>
    <div className="overflow-x-auto rounded-2xl border border-line bg-white">
      <table className="w-full text-left text-xs"><thead><tr className="text-[10px] uppercase tracking-wider text-muted"><th className="p-3">Product</th><th className="p-3 text-right">Earlier reviewed</th><th className="p-3 text-right">Later reviewed</th><th className="p-3 text-right">Change</th></tr></thead>
        <tbody>{rows.map((r) => <tr key={r.sku} className="border-t border-line"><td className="p-3 font-semibold">{r.name}<span className="block font-mono text-[10px] font-normal text-muted">{r.sku}</span></td><td className="p-3 text-right font-mono">{r.before}</td><td className="p-3 text-right font-mono">{r.after}</td><td className={`p-3 text-right font-mono font-bold ${r.delta < 0 ? 'text-danger' : r.delta > 0 ? 'text-brand' : 'text-muted'}`}>{r.delta > 0 ? '+' : ''}{r.delta}</td></tr>)}
        {rows.length === 0 && <tr><td colSpan={4} className="p-6 text-center text-muted">Load two scans to compare counts.</td></tr>}</tbody>
      </table>
    </div>
  </div>;
}
