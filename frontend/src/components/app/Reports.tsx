import { useEffect, useState } from 'react';
import { reports } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import { btnSecondary } from '../ui';
import { Notice, Spinner, errMsg } from './shared';

type Summary = Awaited<ReturnType<typeof reports.summary>>;
type Low = Awaited<ReturnType<typeof reports.lowStock>>;

export function ReportsPage() {
  const { can } = useAuth();
  const [sum, setSum] = useState<Summary | null>(null);
  const [low, setLow] = useState<Low | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    reports.summary().then(setSum).catch((e) => setError(errMsg(e)));
    reports.lowStock().then(setLow).catch((e) => setError(errMsg(e)));
  }, []);
  const max = Math.max(1, ...(sum?.shrinkage_trend ?? [0]));
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold tracking-tight">Reports & low-stock alerts</h1>
        {can('reports.export') && <button type="button" className={btnSecondary} onClick={() => reports.exportLowStock().catch((e) => setError(errMsg(e)))}>Export low stock CSV</button>}
      </div>
      {error && <Notice>{error}</Notice>}
      {!sum && !error && <Spinner />}
      {sum && (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            {([['Units short', sum.total_shrinkage], ['SKUs short', sum.short_skus], ['SKUs over', sum.surplus_skus], ['Auto-resolved', `${sum.accuracy}%`]] as const).map(([l, v]) => (
              <div key={l} className="rounded-2xl border border-line bg-white p-4"><p className="text-[10px] font-bold uppercase tracking-wider text-muted">{l}</p><p className="mt-1 font-mono text-3xl font-bold tabular-nums">{v}</p></div>
            ))}
          </div>
          <div className="rounded-2xl border border-line bg-white p-4">
            <p className="mb-3 text-xs font-bold">Units short per day (last 8 days)</p>
            <div className="flex h-28 items-end gap-2" role="img" aria-label={`Units short per day: ${sum.shrinkage_trend.join(', ')}`}>
              {sum.shrinkage_trend.map((v, i) => (
                <div key={i} className="flex flex-1 flex-col items-center justify-end gap-1">
                  <span className="font-mono text-[10px] tabular-nums">{v}</span>
                  <div className="w-full rounded-t bg-danger/70" style={{ height: `${(v / max) * 80 + (v ? 4 : 1)}%` }} />
                </div>
              ))}
            </div>
          </div>
        </>
      )}
      <div className="overflow-x-auto rounded-2xl border border-line bg-white">
        <table className="w-full text-left text-sm">
          <thead><tr className="text-[10px] uppercase tracking-wider text-muted"><th className="p-3">Product</th><th className="p-3 text-right">On hand</th><th className="p-3 text-right">Minimum</th><th className="p-3 text-right">Suggested reorder</th></tr></thead>
          <tbody>
            {low?.map((l) => <tr key={l.sku} className="border-t border-line"><td className="p-3"><b>{l.name}</b><span className="block font-mono text-xs text-muted">{l.sku}</span></td><td className="p-3 text-right font-mono tabular-nums">{l.on_hand}</td><td className="p-3 text-right font-mono tabular-nums">{l.min_qty}</td><td className="p-3 text-right font-mono font-bold tabular-nums">{l.reorder}</td></tr>)}
            {low?.length === 0 && <tr><td colSpan={4} className="p-8 text-center text-muted">Nothing is below its minimum. Alerts use the latest approved count per shelf.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
