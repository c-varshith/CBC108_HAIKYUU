import { useCallback, useEffect, useRef, useState } from 'react';
import { catalog } from '../../lib/api';
import type { CreditItem } from '../../lib/contract';
import { btnSecondary } from '../ui';
import { Notice, Spinner, errMsg, safeHref } from './shared';

const PAGE = 50;
const field = 'w-full rounded-xl border border-line-strong bg-white px-3 py-2.5 text-sm';
const link = 'font-semibold text-brand underline';

/** Open Food Facts attribution: photos are CC BY-SA 3.0 (keep the credit, share alike), product data is ODbL. Open to every signed-in role. */
export function CreditsPage() {
  const [q, setQ] = useState('');
  const [dq, setDq] = useState('');
  const [items, setItems] = useState<CreditItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  useEffect(() => { const t = window.setTimeout(() => setDq(q.trim()), 300); return () => window.clearTimeout(t); }, [q]);

  const fetchPage = useCallback((offset: number) => catalog.credits({ q: dq || undefined, limit: PAGE, offset }), [dq]);

  useEffect(() => {
    const id = ++seq.current;
    setError(null);
    fetchPage(0).then((r) => { if (id === seq.current) { setItems(r.items); setTotal(r.total); } })
      .catch((e) => { if (id === seq.current) { setError(errMsg(e)); setItems((c) => c ?? []); } });
  }, [fetchPage]);

  async function more() {
    if (!items) return;
    const id = seq.current;
    setLoadingMore(true);
    try {
      const r = await fetchPage(items.length);
      if (id !== seq.current) return;
      setItems((c) => [...(c ?? []), ...r.items]);
      setTotal(r.total);
    } catch (e) { setError(errMsg(e)); } finally { setLoadingMore(false); }
  }

  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-bold tracking-tight">Image credits</h1>
      <section className="space-y-3 rounded-2xl border border-line bg-white p-4 text-sm leading-relaxed">
        <p><b>Product photos.</b> Product photos imported from <a className={link} href="https://world.openfoodfacts.org" target="_blank" rel="noopener noreferrer">openfoodfacts.org</a> are
          licensed under <a className={link} href="https://creativecommons.org/licenses/by-sa/3.0/" target="_blank" rel="noopener noreferrer">Creative Commons Attribution-ShareAlike 3.0 (CC BY-SA 3.0)</a>.
          They were contributed by the Open Food Facts community. If you reuse or republish them you must keep the credit shown below and share your result under the same licence.</p>
        <p><b>Product data.</b> Names, brands, barcodes and quantities come from Open Food Facts, which makes its database available under the
          {' '}<a className={link} href="https://opendatacommons.org/licenses/odbl/1-0/" target="_blank" rel="noopener noreferrer">Open Database License (ODbL) 1.0</a>.
          If you publish an adapted version of this data, you must credit Open Food Facts and offer it under the same licence.</p>
        <p className="text-xs text-muted">Photos you upload yourself are not listed here: they belong to you. Open Food Facts is not affiliated with and does not endorse this service.</p>
      </section>

      <div className="flex flex-wrap items-center gap-3">
        <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search by SKU or name" aria-label="Search credits" className={`${field} max-w-sm`} />
        {items && <span className="text-xs text-muted" role="status">{total} credited product{total === 1 ? '' : 's'}{dq ? ' match' : ''}</span>}
      </div>
      {error && <Notice>{error}</Notice>}
      {!items && !error && <Spinner />}
      {items && items.length === 0 && !error && <p className="py-6 text-center text-sm text-muted">{dq ? 'No credited product matches this search.' : 'No products with imported images yet.'}</p>}
      {items && items.length > 0 && (
        <div className="overflow-x-auto rounded-2xl border border-line bg-white">
          <table className="w-full text-left text-xs">
            <thead><tr className="text-[10px] uppercase tracking-wider text-muted"><th className="p-3">SKU</th><th className="p-3">Product</th><th className="p-3">Attribution</th><th className="p-3">Source</th></tr></thead>
            <tbody>
              {items.map((i) => (
                <tr key={i.sku} className="border-t border-line align-top">
                  <td className="p-3 font-mono">{i.sku}</td>
                  <td className="p-3 font-semibold">{i.name}{i.brand && <span className="block text-[10px] font-normal text-muted">{i.brand}</span>}</td>
                  <td className="p-3">{i.attribution ?? '—'}<span className="block text-[10px] text-muted">{i.license}</span></td>
                  <td className="p-3">{safeHref(i.source_url) ? <a className={link} href={safeHref(i.source_url)} target="_blank" rel="noopener noreferrer">Open Food Facts page</a> : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {items && items.length < total && <div className="text-center"><button type="button" className={btnSecondary} disabled={loadingMore} onClick={() => void more()}>{loadingMore ? 'Loading…' : `Load more (${total - items.length} left)`}</button></div>}
    </div>
  );
}
