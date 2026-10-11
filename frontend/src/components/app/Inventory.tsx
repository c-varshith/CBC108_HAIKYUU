import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, ArrowDownUp, ArrowUp } from 'lucide-react';
import { reports } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import { Spinner, Notice, errMsg, fmtDate, go } from './shared';
import { btnSecondary } from '../ui';

type Inventory = Awaited<ReturnType<typeof reports.inventory>>;
type SortKey = 'product' | 'counted' | 'expected' | 'variance' | 'minimum' | 'status';
type SortDirection = 'asc' | 'desc';

const statusLabel = (status: Inventory['items'][number]['status']) => ({
  LOW: 'Low stock', NOT_COUNTED: 'Not counted', PARTIAL: 'Partial data', IN_STOCK: 'In stock',
}[status]);

export function InventoryPage() {
  const { user, can } = useAuth();
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [query, setQuery] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const loading = useRef(false);
  const [sort, setSort] = useState<{ key: SortKey; direction: SortDirection }>({ key: 'product', direction: 'asc' });

  const refresh = useCallback(async () => {
    if (loading.current) return;
    loading.current = true;
    setRefreshing(true);
    try { setInventory(await reports.inventory()); setError(null); }
    catch (e) { setError(errMsg(e)); }
    finally { loading.current = false; setRefreshing(false); }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const items = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = (inventory?.items ?? []).filter((item) => !q || `${item.name} ${item.sku}`.toLowerCase().includes(q));
    return filtered.sort((a, b) => {
      if (sort.key === 'product') return (sort.direction === 'asc' ? 1 : -1) * (a.name.localeCompare(b.name, undefined, { sensitivity: 'base', numeric: true }) || a.sku.localeCompare(b.sku));
      if (sort.key === 'status') return (sort.direction === 'asc' ? 1 : -1) * statusLabel(a.status).localeCompare(statusLabel(b.status));
      const aValue = sort.key === 'counted' ? a.on_hand : sort.key === 'expected' ? a.expected_qty : sort.key === 'variance' ? a.variance : a.min_qty;
      const bValue = sort.key === 'counted' ? b.on_hand : sort.key === 'expected' ? b.expected_qty : sort.key === 'variance' ? b.variance : b.min_qty;
      if (aValue === null || bValue === null) return aValue === bValue ? 0 : aValue === null ? 1 : -1;
      return (sort.direction === 'asc' ? 1 : -1) * (aValue - bValue || a.sku.localeCompare(b.sku));
    });
  }, [inventory, query, sort]);

  const sortHeader = (label: string, key: SortKey, align: 'left' | 'right' = 'left') => {
    const active = sort.key === key;
    const Icon = active ? sort.direction === 'asc' ? ArrowUp : ArrowDown : ArrowDownUp;
    return <th scope="col" aria-sort={active ? sort.direction === 'asc' ? 'ascending' : 'descending' : 'none'} className={`p-3 ${align === 'right' ? 'text-right' : ''}`}>
      <button type="button" onClick={() => setSort((current) => ({ key, direction: current.key === key && current.direction === 'asc' ? 'desc' : 'asc' }))}
        aria-label={`${active ? `Sorted ${sort.direction === 'asc' ? 'ascending' : 'descending'} by` : 'Sort by'} ${label}`}
        className={`inline-flex items-center gap-1.5 rounded-md hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand ${align === 'right' ? 'flex-row-reverse' : ''}`}>
        {label}<Icon className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
    </th>;
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Inventory</h1>
          <p className="mt-1 text-sm text-muted">Catalog-wide product counts from the latest approved scan of each shelf.</p>
        </div>
        <button type="button" disabled={refreshing} onClick={() => void refresh()} className="rounded-full border border-line-strong bg-white px-4 py-2 text-sm font-semibold hover:border-brand disabled:opacity-50">
          {refreshing ? 'Refreshing…' : 'Refresh counts'}
        </button>
      </div>
      {error && <Notice>{error}</Notice>}
      {!inventory && !error && <Spinner />}
      {inventory && <>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {([
            ['Catalog products', inventory.product_count],
            ['Counted units', inventory.total_on_hand ?? '—'],
            ['Expected units', inventory.total_expected],
            ['Shelves counted', `${inventory.counted_shelves} / ${inventory.total_shelves}`],
          ] as const).map(([label, value]) => (
            <div key={label} className="rounded-2xl border border-line bg-white p-4">
              <p className="text-[10px] font-bold uppercase tracking-wider text-muted">{label}</p>
              <p className="mt-1 font-mono text-3xl font-bold tabular-nums">{value}</p>
            </div>
          ))}
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-line bg-white px-4 py-3 text-xs text-muted">
          <span>{inventory.counted_shelves === 0
            ? inventory.total_shelves === 0
              ? 'No shelves are configured in this workspace yet. Inventory counts appear after a shelf has an approved scan.'
              : 'No approved shelf counts yet. Approve a reviewed scan to populate current quantities.'
            : inventory.counted_shelves < inventory.total_shelves
              ? `Counts include ${inventory.counted_shelves} of ${inventory.total_shelves} shelves. Quantities exclude unscanned shelves; variance and stock status are partial.`
              : 'All shelves have approved counts. Quantities and variance cover the full workspace.'}</span>
          {inventory.last_counted_at && <span>Last approved count · {fmtDate(inventory.last_counted_at)}</span>}
        </div>
        {inventory.product_count === 0 && <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-warn/30 bg-warn-soft p-4 text-sm">
          <p className="max-w-3xl">This workspace has no active catalog products, so there are no product rows to show. Inventory data is private to each workspace. A newly signed-up account starts in a separate empty workspace and won’t see another workspace’s catalog or stock. {user?.role === 'STAFF' || user?.role === 'AUDITOR' ? 'If you expected an existing store’s inventory, sign in with an account that belongs to that workspace.' : 'Add products to this workspace catalog to begin tracking them.'}</p>
          {can('catalog.write') && <button type="button" className={btnSecondary} onClick={() => go('catalog')}>Open catalog</button>}
        </div>}
        <label className="block max-w-sm text-xs font-semibold">Find a product
          <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search product or SKU" className="mt-1 w-full rounded-xl border border-line-strong bg-white px-3.5 py-2.5 text-sm font-normal outline-none focus:border-brand focus:ring-2 focus:ring-brand/20" />
        </label>
        <div className="overflow-x-auto rounded-2xl border border-line bg-white">
          <table className="w-full min-w-[680px] text-left text-sm">
            <thead><tr className="text-[10px] uppercase tracking-wider text-muted">
              {sortHeader('Product', 'product')}{sortHeader('Counted', 'counted', 'right')}{sortHeader('Expected', 'expected', 'right')}{sortHeader('Variance', 'variance', 'right')}{sortHeader('Minimum', 'minimum', 'right')}{sortHeader('Status', 'status', 'right')}
            </tr></thead>
            <tbody>
              {items.map((item) => <tr key={item.sku} className="border-t border-line">
                <td className="p-3"><b>{item.name}</b><span className="block font-mono text-xs text-muted">{item.sku}</span></td>
                <td className="p-3 text-right font-mono tabular-nums">{item.on_hand ?? 'Not counted'}</td>
                <td className="p-3 text-right font-mono tabular-nums">{item.expected_qty}</td>
                <td className={`p-3 text-right font-mono tabular-nums ${item.variance !== null && item.variance < 0 ? 'text-danger' : ''}`}>{item.variance === null ? '—' : item.variance > 0 ? `+${item.variance}` : item.variance}</td>
                <td className="p-3 text-right font-mono tabular-nums">{item.min_qty}</td>
                <td className="p-3 text-right"><span className={`inline-flex rounded-full px-2.5 py-1 text-[10px] font-bold uppercase ${item.status === 'LOW' ? 'bg-danger-soft text-danger' : item.status === 'NOT_COUNTED' || item.status === 'PARTIAL' ? 'bg-cream text-muted' : 'bg-brand-soft text-brand-deep'}`}>
                  {item.status === 'LOW' ? 'Low stock' : item.status === 'NOT_COUNTED' ? 'Not counted' : item.status === 'PARTIAL' ? 'Partial data' : 'In stock'}
                </span></td>
              </tr>)}
              {items.length === 0 && <tr><td colSpan={6} className="p-8 text-center text-muted">{query ? 'No products match your search.' : 'No active catalog products yet.'}</td></tr>}
            </tbody>
          </table>
          <p className="border-t border-line px-3 py-2 text-center text-xs text-muted">Showing {items.length} of {inventory.product_count} active catalog products.</p>
        </div>
      </>}
    </div>
  );
}
