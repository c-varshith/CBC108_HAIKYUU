import { useCallback, useEffect, useRef, useState } from 'react';
import { reports } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import { btnSecondary } from '../ui';
import { Notice, Spinner, errMsg, fmtDate } from './shared';

type Summary = Awaited<ReturnType<typeof reports.summary>>;
type Low = Awaited<ReturnType<typeof reports.lowStock>>;

export function ReportsPage() {
  const { can } = useAuth();
  const [sum, setSum] = useState<Summary | null>(null);
  const [low, setLow] = useState<Low | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const loading = useRef(false);

  const refresh = useCallback(async () => {
    if (loading.current) return;
    loading.current = true;
    setRefreshing(true);
    const [summaryResult, lowResult] = await Promise.allSettled([reports.summary(), reports.lowStock()]);
    const errors: string[] = [];
    if (summaryResult.status === 'fulfilled') setSum(summaryResult.value);
    else errors.push(`Report summary: ${errMsg(summaryResult.reason)}`);
    if (lowResult.status === 'fulfilled') setLow(lowResult.value);
    else errors.push(`Low-stock alerts: ${errMsg(lowResult.reason)}`);
    setError(errors.length ? errors.join(' · ') : null);
    if (summaryResult.status === 'fulfilled' || lowResult.status === 'fulfilled') setUpdatedAt(new Date().toISOString());
    loading.current = false;
    setRefreshing(false);
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const days = sum?.shrinkage_days ?? [];
  const max = Math.max(1, ...days.map((day) => day.units_short));
  const dateLabel = (value: string) => {
    const [year, month, day] = value.split('-').map(Number);
    return new Date(year, month - 1, day).toLocaleDateString(undefined, { month: 'numeric', day: 'numeric' });
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Reports & low-stock alerts</h1>
          <p className="mt-1 text-xs text-muted">{updatedAt ? `Updated ${fmtDate(updatedAt)} · refreshes every 15 seconds` : 'Loading current workspace data…'}</p>
        </div>
        <div className="flex items-center gap-2">
          <button type="button" className={btnSecondary} disabled={refreshing} onClick={() => void refresh()}>{refreshing ? 'Refreshing…' : 'Refresh reports'}</button>
          {can('reports.export') && <button type="button" className={btnSecondary} onClick={() => reports.exportLowStock().catch((e) => setError(errMsg(e)))}>Export low stock CSV</button>}
        </div>
      </div>
      {error && <Notice>{error}</Notice>}
      {!sum && !error && <Spinner />}
      {sum && (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            {([
              ['Units short', sum.total_shrinkage, 'Across the latest approved count for each shelf'],
              ['SKUs short', sum.short_skus, 'Catalog products below expected count'],
              ['SKUs over', sum.surplus_skus, 'Catalog products above expected count'],
              ['Auto-resolved · 30d', `${sum.accuracy}%`, `${sum.auto_resolved_boxes.toLocaleString()} of ${sum.processed_boxes.toLocaleString()} processed boxes`],
            ] as const).map(([label, value, detail]) => (
              <div key={label} className="rounded-2xl border border-line bg-white p-4">
                <p className="text-[10px] font-bold uppercase tracking-wider text-muted">{label}</p>
                <p className="mt-1 font-mono text-3xl font-bold tabular-nums">{value}</p>
                <p className="mt-1 text-[11px] text-muted">{detail}</p>
              </div>
            ))}
          </div>
          <div className="rounded-2xl border border-line bg-white p-4">
            <p className="mb-1 text-xs font-bold">Units short from approved scans · last 8 days</p>
            <p className="mb-3 text-[11px] text-muted">Daily short units summed across approved scans. Days without an approved scan are shown as —.</p>
            <div className="flex h-36 items-end gap-2" role="img" aria-label={`Units short per day: ${days.map((day) => `${day.date}: ${day.approved_scans ? day.units_short : 'no approved scan'}`).join(', ')}`}>
              {days.map((day) => (
                <div key={day.date} title={`${dateLabel(day.date)}: ${day.approved_scans ? `${day.units_short} units short across ${day.approved_scans} approved scan${day.approved_scans === 1 ? '' : 's'}` : 'No approved scans'}`} className="flex min-w-0 flex-1 flex-col items-center gap-1">
                  <span className="font-mono text-[10px] tabular-nums">{day.approved_scans ? day.units_short : '—'}</span>
                  <div className="flex h-24 w-full items-end border-b border-line">
                    {day.approved_scans > 0 && <div className="w-full rounded-t bg-danger/70" style={{ height: `${Math.max(day.units_short ? 8 : 2, (day.units_short / max) * 100)}%` }} />}
                  </div>
                  <span className="text-[10px] text-muted">{dateLabel(day.date)}</span>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
      <div className="overflow-x-auto rounded-2xl border border-line bg-white">
        <table className="w-full text-left text-sm">
          <thead><tr className="text-[10px] uppercase tracking-wider text-muted"><th className="p-3">Product</th><th className="p-3 text-right">On hand</th><th className="p-3 text-right">Minimum</th><th className="p-3 text-right">Order to minimum</th></tr></thead>
          <tbody>
            {low?.map((item) => <tr key={item.sku} className="border-t border-line"><td className="p-3"><b>{item.name}</b><span className="block font-mono text-xs text-muted">{item.sku}</span></td><td className="p-3 text-right font-mono tabular-nums">{item.on_hand}</td><td className="p-3 text-right font-mono tabular-nums">{item.min_qty}</td><td className="p-3 text-right font-mono font-bold tabular-nums">{item.reorder}</td></tr>)}
            {low?.length === 0 && <tr><td colSpan={4} className="p-8 text-center text-muted">Nothing is below its minimum. Alerts use the latest approved count per shelf.</td></tr>}
            {!low && <tr><td colSpan={4} className="p-8 text-center text-muted">{error ? 'Low-stock data could not be loaded.' : 'Loading low-stock alerts…'}</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
