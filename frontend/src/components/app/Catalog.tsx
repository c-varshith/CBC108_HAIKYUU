import { AlertTriangle, CheckCircle2, Clock, ImagePlus, RefreshCw } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { catalog, stock } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import type { Batch, ExpectedRow, IndexStatus, Product, ProductStatus, RecognitionPreview, Shelf } from '../../lib/contract';
import { cn } from '../../utils/cn';
import { btnPrimary, btnSecondary } from '../ui';
import { Notice, Spinner, errMsg, fmtDate, go, safeHref } from './shared';

const field = 'w-full rounded-xl border border-line-strong bg-white px-3 py-2.5 text-sm';
const PAGE = 24; // products per "Load more" page; the list asks for PAGE + 1 to learn whether another page exists
const MAX_AT_ONCE = 10; // photos per request (server limit)
const MAX_PER_PRODUCT = 20; // photos per product (server limit)

/** Index state as the SERVER reports it. Icon + words, never colour alone. */
const BADGE: Record<ProductStatus, { label: string; cls: string; Icon: typeof Clock }> = {
  ready: { label: 'Ready', cls: 'bg-brand-soft text-brand-deep', Icon: CheckCircle2 },
  pending: { label: 'Not searchable yet', cls: 'bg-warn-soft text-warn', Icon: Clock },
  failed: { label: 'Failed', cls: 'bg-danger-soft text-danger', Icon: AlertTriangle },
};
export function IndexBadge({ status }: { status: ProductStatus }) {
  const b = BADGE[status];
  return <span className={cn('inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide', b.cls)}><b.Icon className="h-3 w-3" aria-hidden />{b.label}</span>;
}

/** What to do for a product the matcher has not confirmed: photos stored -> ask the server to re-send them; no photos -> upload the kept files again. */
async function learn(p: Product, files: File[], onUploaded: () => void): Promise<Product> {
  if (p.reference_count > 0 && p.status === 'failed') { onUploaded(); return catalog.reindex(p.sku); }
  return catalog.addReferences(p.sku, files, onUploaded);
}

function Register({ onDone }: { onDone: () => void }) {
  const [sku, setSku] = useState('');
  const [name, setName] = useState('');
  const [minQty, setMinQty] = useState('0');
  const [files, setFiles] = useState<File[]>([]);
  const [created, setCreated] = useState<Product | null>(null); // the product exists on the server but may not be searchable yet
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [phase, setPhase] = useState<'idle' | 'uploading' | 'learning' | 'ready' | 'failed'>('idle');
  const pick = useRef<HTMLInputElement>(null);

  function reset() { setSku(''); setName(''); setMinQty('0'); setFiles([]); setCreated(null); setError(null); setOk(null); setPhase('idle'); }

  async function submit(e: FormEvent) {
    e.preventDefault();
    const code = sku.trim();
    if (!created && files.length < 1) { setError('Add at least one reference photo (3 to 5 works best).'); return; }
    if (created && created.reference_count === 0 && files.length < 1) { setError('Add at least one reference photo.'); return; }
    setBusy(true); setError(null); setOk(null); setPhase('uploading');
    // Two calls: create the product, then teach the matcher. If the second fails the product stays, the chosen photos are kept and Retry resumes.
    let p = created && created.sku === code ? created : null;
    try {
      if (!p) { p = await catalog.create({ sku: code, name: name.trim(), min_qty: Number(minQty) || 0 }); setCreated(p); onDone(); }
      const done = await learn(p, files, () => setPhase('learning'));
      setCreated(done);
      setPhase('ready');
      setFiles([]);
      setOk(`${done.name} is saved in the catalog and ready to recognize in the next scan. No retraining needed.`);
      onDone();
    } catch (err) {
      if (p) {
        setPhase('failed');
        setError(`${errMsg(err)} The product and photos are saved, but it is not searchable yet. Press Retry when the matching service is available.`);
        catalog.product(p.sku).then(setCreated).catch(() => undefined); // show the actual server state
        onDone();
      } else { setPhase('failed'); setError(errMsg(err)); }
    } finally { setBusy(false); }
  }

  const room = MAX_AT_ONCE - files.length;
  return (
    <form onSubmit={submit} className="space-y-3 rounded-2xl border border-line bg-white p-4">
      <h2 className="text-sm font-bold">Register a product with reference photos</h2>
      <div className="grid gap-3 sm:grid-cols-3">
        <input className={field} placeholder="SKU code" aria-label="SKU code" value={sku} onChange={(e) => setSku(e.target.value)} readOnly={!!created} required />
        <input className={`${field} sm:col-span-2`} placeholder="Product name" aria-label="Product name" value={name} onChange={(e) => setName(e.target.value)} readOnly={!!created} required />
      </div>
      <label className="block text-xs font-semibold">Low-stock minimum<input className={`${field} mt-1`} type="number" min={0} value={minQty} onChange={(e) => setMinQty(e.target.value)} readOnly={!!created} /></label>
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" className={btnSecondary} disabled={room <= 0} onClick={() => pick.current?.click()}><ImagePlus className="h-4 w-4" />Add photos ({files.length})</button>
        <input ref={pick} type="file" accept="image/*" multiple hidden onChange={(e) => {
          const picked = Array.from(e.target.files ?? []); e.target.value = '';
          if (picked.length > room) setError(`You can add up to ${MAX_AT_ONCE} photos at once; the first ${Math.max(room, 0)} were kept.`);
          setFiles((c) => [...c, ...picked].slice(0, MAX_AT_ONCE));
        }} />
        {files.length > 0 && <button type="button" className="text-xs font-semibold text-muted underline" onClick={() => setFiles([])}>Clear photos</button>}
        <span className="text-xs text-muted">3 to 5 photos from different angles work best. Use clear, well-lit photos with the full package visible and varied backgrounds. Learning takes seconds; no retraining.</span>
      </div>
      {phase !== 'idle' && <ol aria-label="Product learning progress" className="grid gap-2 rounded-xl bg-cream p-3 text-xs sm:grid-cols-3">
        <li className={cn('font-semibold', ['uploading', 'learning', 'ready'].includes(phase) ? 'text-brand-deep' : 'text-muted')}>1. Photos uploaded</li>
        <li className={cn('font-semibold', phase === 'learning' ? 'text-brand-deep' : phase === 'ready' ? 'text-brand-deep' : 'text-muted')}>2. Learning product photos</li>
        <li className={cn('font-semibold', phase === 'ready' ? 'text-brand-deep' : 'text-muted')}>3. Ready for next scan</li>
        {created && <li className="sm:col-span-3">Server status: <IndexBadge status={created.status} /></li>}
        {phase === 'failed' && <li className="font-semibold text-danger sm:col-span-3">Learning failed. Photos remain saved so you can retry.</li>}
      </ol>}
      {created && <p className="flex flex-wrap items-center gap-2 text-xs" role="status"><IndexBadge status={created.status} /><span className="font-mono">{created.sku}</span>
        <button type="button" className="font-semibold text-muted underline" onClick={reset}>Start over</button></p>}
      {error && <Notice>{error}</Notice>}{ok && <Notice tone="brand">{ok}</Notice>}
      <div className="flex flex-wrap items-center gap-3">
        <button type="submit" disabled={busy} className={btnPrimary}>{busy ? phase === 'learning' ? 'Learning product photos…' : 'Uploading photos…' : phase === 'failed' ? 'Retry learning' : created ? 'Retry' : 'Register product'}</button>
        {created && <button type="button" className="text-xs font-semibold text-muted underline" onClick={reset}>Start over</button>}
      </div>
    </form>
  );
}

/** Add more photos to a product. A failed upload keeps the chosen files so the user can retry without picking them again. */
function AddPhotos({ p, onChanged }: { p: Product; onChanged: (p: Product) => void }) {
  const pick = useRef<HTMLInputElement>(null);
  const [kept, setKept] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const room = Math.min(MAX_AT_ONCE, MAX_PER_PRODUCT - p.reference_count);

  async function send(files: File[]) {
    setBusy(true); setErr(null);
    try { onChanged(await catalog.addReferences(p.sku, files)); setKept([]); }
    catch (x) { setKept(files); setErr(errMsg(x)); }
    finally { setBusy(false); }
  }
  return (
    <span className="text-right">
      {kept.length > 0 && !busy
        ? <><button type="button" className="text-xs font-semibold text-brand underline" onClick={() => send(kept)}>Retry upload ({kept.length})</button>
          <button type="button" className="ml-2 text-xs font-semibold text-muted underline" onClick={() => { setKept([]); setErr(null); }}>Discard</button></>
        : <button type="button" disabled={busy || room <= 0} className="text-xs font-semibold text-brand underline disabled:text-muted disabled:no-underline" onClick={() => pick.current?.click()}
          title={room <= 0 ? `A product can have at most ${MAX_PER_PRODUCT} photos` : undefined}>{busy ? 'Learning…' : 'Add photos'}</button>}
      <input ref={pick} type="file" accept="image/*" multiple hidden onChange={(e) => {
        const f = Array.from(e.target.files ?? []); e.target.value = ''; if (!f.length) return;
        if (f.length > room) { setErr(`You can add up to ${room} more photo${room === 1 ? '' : 's'} to this product.`); return; }
        void send(f);
      }} />
      {err && <span className="block text-xs text-danger" role="alert">{err}</span>}
    </span>
  );
}

function TestRecognition({ p }: { p: Product }) {
  const pick = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<RecognitionPreview | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(file: File) {
    setBusy(true); setError(null); setResult(null);
    try { setResult(await catalog.testRecognition(p.sku, file)); }
    catch (e) { setError(errMsg(e)); }
    finally { setBusy(false); }
  }
  return <div className="mt-3 border-t border-line pt-3">
    <input ref={pick} type="file" accept="image/jpeg,image/png,image/webp,image/heic" hidden onChange={(e) => {
      const file = e.target.files?.[0]; e.target.value = ''; if (file) void run(file);
    }} />
    <button type="button" className="text-xs font-semibold text-brand underline disabled:text-muted" disabled={busy} onClick={() => pick.current?.click()}>
      {busy ? 'Testing recognition…' : 'Test recognition'}
    </button>
    <p className="mt-1 text-[10px] text-muted">Try a photo of this product. This preview does not create a scan.</p>
    {error && <p role="alert" className="mt-2 text-xs text-danger">{error}</p>}
    {result && <div className="mt-2 space-y-2" role="status">
      {result.detections.length === 0 && <p className="text-xs text-muted">No product boxes found. Try a clearer, well-lit photo with the package fully visible.</p>}
      {result.detections.map((d, i) => <div key={i} className="rounded-lg bg-cream p-2 text-xs">
        <p className="font-semibold">Detected item {i + 1}{d.target_score === null ? ' · target product not in top matches' : ` · ${p.name}: ${Math.round(d.target_score * 100)}% similarity`}</p>
        <ul className="mt-1 space-y-0.5 font-mono text-muted">{d.matches.map((m) => <li key={m.sku}>{m.name} ({m.sku}) · {Math.round(m.score * 100)}% similarity</li>)}</ul>
        {d.match_probability !== null && <p className="mt-1 text-muted">Calibrated match probability: {Math.round(d.match_probability * 100)}%</p>}
        {d.close_match && <p className="mt-1 font-semibold text-warn">Similar products scored close together. Add varied reference photos or review the catalog matches.</p>}
      </div>)}
    </div>}
  </div>;
}

function RetryIndex({ p, onChanged }: { p: Product; onChanged: (p: Product) => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="mt-2 space-y-1" role="status">
      <p className="text-xs text-danger">The photos are saved, but the matching service did not confirm them, so this product cannot be recognised yet.</p>
      <button type="button" disabled={busy} className={btnSecondary} onClick={async () => {
        setBusy(true); setErr(null);
        try { onChanged(await catalog.reindex(p.sku)); } catch (x) { setErr(errMsg(x)); } finally { setBusy(false); }
      }}><RefreshCw className="h-4 w-4" />{busy ? 'Retrying…' : 'Retry'}</button>
      {err && <p className="text-xs text-danger" role="alert">{err}</p>}
    </div>
  );
}

function Requests({ write }: { write: boolean }) {
  const [rows, setRows] = useState<Awaited<ReturnType<typeof catalog.requests>>>([]);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => { catalog.requests().then(setRows).catch((e) => setError(errMsg(e))); }, []);
  useEffect(load, [load]);
  const shown = write ? rows.filter((r) => r.status === 'PENDING') : rows;
  if (shown.length === 0 && !error) return null;
  return (
    <section className="space-y-2 rounded-2xl border border-line bg-white p-4">
      <h2 className="text-sm font-bold">{write ? 'New product requests' : 'My product requests'}</h2>
      {error && <Notice>{error}</Notice>}
      <ul className="divide-y divide-line text-sm">
        {shown.map((r) => (
          <li key={r.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
            <span><b>{r.name}</b> <span className="text-xs text-muted">requested by {r.requested_by.name} · {r.status.toLowerCase()}</span></span>
            {write && r.status === 'PENDING' && <span className="flex gap-2">
              <button type="button" className={btnSecondary} onClick={() => catalog.decide(r.id, 'APPROVED').then(load).catch((e) => setError(errMsg(e)))}>Approve</button>
              <button type="button" className={btnSecondary} onClick={() => catalog.decide(r.id, 'REJECTED').then(load).catch((e) => setError(errMsg(e)))}>Reject</button>
            </span>}
          </li>
        ))}
      </ul>
      {write && <p className="text-xs text-muted">Approving a request does not create the product: register it above with reference photos.</p>}
    </section>
  );
}

/** "3 created · 2 failed" from a batch's numeric counts (everything the importer or resync reported, zero values left out). */
function describeCounts(c: Batch['counts']): string {
  const parts = Object.entries(c).filter(([, v]) => typeof v === 'number' && v > 0).map(([k, v]) => `${v} ${k.replace(/_/g, ' ')}`);
  return parts.length ? parts.join(' · ') : 'nothing to report';
}

/** Owner only: is the search index healthy, and a button to rebuild it from the stored photos (no retraining; upserts, search keeps working). */
function IndexPanel({ onResynced }: { onResynced: () => void }) {
  const [st, setSt] = useState<IndexStatus | null>(null);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [cur, setCur] = useState<Batch | null>(null); // the resync this page started
  const [active, setActive] = useState<string | null>(null); // batch id being polled
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { const [s, b] = await Promise.all([catalog.indexStatus(), catalog.batches()]); setSt(s); setBatches(b.slice(0, 5)); }
    catch (e) { setError(errMsg(e)); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!active) return;
    let alive = true;
    const tick = async () => {
      try {
        const b = await catalog.batch(active);
        if (!alive) return;
        setCur(b);
        if (!b.running) { setActive(null); void load(); onResynced(); }
      } catch (e) { if (alive) { setError(errMsg(e)); setActive(null); } }
    };
    void tick();
    const t = window.setInterval(tick, 2000);
    return () => { alive = false; window.clearInterval(t); };
  }, [active, load, onResynced]);

  async function start() {
    setBusy(true); setError(null); setConfirm(false); setCur(null);
    try { setActive((await catalog.resync()).batch_id); }
    catch (e) { setError(errMsg(e)); void load(); } finally { setBusy(false); }
  }

  const running = !!active;
  const qdrantLost = !!st?.warnings.some((w) => /no vectors|holds no vectors|Qdrant was wiped/i.test(w));
  const counts = st?.products;
  return (
    <section className="space-y-3 rounded-2xl border border-line bg-white p-4" aria-label="Search index">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-bold">Search index</h2>
        <button type="button" className="text-xs font-semibold text-muted underline" onClick={() => void load()}>Refresh</button>
      </div>
      {error && <Notice>{error}</Notice>}
      {st && <>
        <p className="text-xs text-muted">
          <b className="text-ink">{counts?.ready ?? 0}</b> ready · <b className="text-ink">{counts?.pending ?? 0}</b> without photos · <b className={cn(counts?.failed ? 'text-danger' : 'text-ink')}>{counts?.failed ?? 0}</b> failed
          {st.model && <> · matcher <span className="font-mono">{st.model}@{st.collection}</span> ({st.worker_references ?? '?'} vectors)</>}
        </p>
        {st.warnings.map((w) => <Notice key={w} tone="warn">{w}</Notice>)}
      </>}
      <div className="flex flex-wrap items-center gap-2">
        {!confirm && <button type="button" disabled={busy || running} className={btnSecondary} onClick={() => qdrantLost ? void start() : setConfirm(true)}><RefreshCw className={cn('h-4 w-4', running && 'animate-spin')} />{running ? 'Re-learning products…' : qdrantLost ? 'Re-learn all products' : 'Resync search index'}</button>}
        {confirm && <>
          <span className="text-xs text-muted">Re-sends every stored photo of every active product to the matching service. This restores vectors lost by in-memory Qdrant. Nothing is retrained.</span>
          <button type="button" className={btnPrimary} onClick={() => void start()}>Re-learn all products</button>
          <button type="button" className={btnSecondary} onClick={() => setConfirm(false)}>Cancel</button>
        </>}
      </div>
      {cur && <div role="status" className="space-y-1 text-xs">
        <p><b>{cur.running ? 'Resync running' : cur.errors.length || Number(cur.counts.failed) > 0 ? 'Resync finished with problems' : 'Resync finished'}:</b> {describeCounts(cur.counts)}</p>
        {(cur.counts.warnings ?? []).map((w) => <Notice key={w} tone="warn">{w}</Notice>)}
        {!cur.running && cur.errors.length > 0 && <ul className="list-disc pl-5 text-danger">{cur.errors.slice(0, 5).map((x, i) => <li key={i}><span className="font-mono">{x.key}</span>: {x.error}</li>)}{cur.errors.length > 5 && <li>and {cur.errors.length - 5} more</li>}</ul>}
      </div>}
      {batches.length > 0 && <div>
        <h3 className="text-[10px] font-bold uppercase tracking-wider text-muted">Recent imports and resyncs</h3>
        <ul className="mt-1 divide-y divide-line text-xs">
          {batches.map((b) => (
            <li key={b.id} className="flex flex-wrap items-baseline justify-between gap-x-3 py-1.5">
              <span><b>{b.source === 'resync' ? 'Resync' : b.source === 'openfoodfacts' ? 'Open Food Facts import' : b.source}</b> <span className="text-muted">{fmtDate(b.started_at)}</span>{b.running && <span className="ml-1 font-semibold text-warn">running</span>}</span>
              <span className="text-muted">{describeCounts(b.counts)}{b.errors.length > 0 && ` · ${b.errors.length} logged`}</span>
            </li>
          ))}
        </ul>
      </div>}
    </section>
  );
}

const FILTERS: { value: ProductStatus | ''; label: string }[] = [
  { value: '', label: 'All products' }, { value: 'ready', label: 'Ready' }, { value: 'pending', label: 'Not searchable yet' }, { value: 'failed', label: 'Failed' },
];

export function CatalogPage() {
  const { can } = useAuth();
  const write = can('catalog.write');
  const [products, setProducts] = useState<Product[] | null>(null);
  const [more, setMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [q, setQ] = useState('');
  const [dq, setDq] = useState(''); // debounced search text
  const [status, setStatus] = useState<ProductStatus | ''>('');
  const [shelves, setShelves] = useState<Shelf[]>([]);
  const [shelf, setShelf] = useState('');
  const [expected, setExpected] = useState<ExpectedRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0); // ignores answers to superseded searches

  useEffect(() => { const t = window.setTimeout(() => setDq(q.trim()), 300); return () => window.clearTimeout(t); }, [q]);

  // limit + 1: if the server returns more than PAGE rows there is a next page, without a separate count query (stays cheap at 30k products)
  const fetchPage = useCallback((offset: number) =>
    catalog.products(write, { q: dq || undefined, status: status || undefined, limit: PAGE + 1, offset }), [write, dq, status]);

  const reload = useCallback(() => {
    const id = ++seq.current;
    setError(null);
    fetchPage(0).then((r) => { if (id === seq.current) { setProducts(r.slice(0, PAGE)); setMore(r.length > PAGE); } })
      .catch((e) => { if (id === seq.current) { setError(errMsg(e)); setProducts((c) => c ?? []); } });
  }, [fetchPage]);
  useEffect(() => { reload(); }, [reload]);

  async function loadMore() {
    if (!products) return;
    const id = seq.current;
    setLoadingMore(true);
    try {
      const r = await fetchPage(products.length);
      if (id !== seq.current) return;
      setProducts((c) => { const have = new Set((c ?? []).map((x) => x.sku)); return [...(c ?? []), ...r.slice(0, PAGE).filter((x) => !have.has(x.sku))]; });
      setMore(r.length > PAGE);
    } catch (e) { setError(errMsg(e)); } finally { setLoadingMore(false); }
  }

  const changed = useCallback((p: Product) => setProducts((c) => c?.map((x) => (x.sku === p.sku ? p : x)) ?? c), []);

  useEffect(() => { catalog.shelves().then((s) => { setShelves(s); setShelf((c) => c || s[0]?.id || ''); }).catch(() => undefined); }, []);
  useEffect(() => { if (shelf) stock.expected(shelf).then(setExpected).catch(() => setExpected([])); }, [shelf]);
  const qty = (sku: string) => expected.find((e) => e.sku === sku)?.expected_qty ?? '';

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-2xl font-bold tracking-tight">{write ? 'Catalog & expected stock' : 'Catalog'}</h1>
        {shelves.length > 0 && <label className="text-xs font-semibold">Expected stock for<select value={shelf} onChange={(e) => setShelf(e.target.value)} className="ml-2 rounded-lg border border-line-strong bg-white px-2.5 py-2 text-sm">{shelves.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>}
      </div>
      {write && <Register onDone={reload} />}
      {can('catalog.resync') && <IndexPanel onResynced={reload} />}
      <Requests write={write} />
      <div className="flex flex-wrap items-center gap-3">
        <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search SKU, name, brand or barcode" aria-label="Search products" className={`${field} max-w-sm`} />
        <label className="text-xs font-semibold">Show
          <select value={status} onChange={(e) => setStatus(e.target.value as ProductStatus | '')} aria-label="Filter by status" className="ml-2 rounded-lg border border-line-strong bg-white px-2.5 py-2 text-sm">
            {FILTERS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </label>
      </div>
      {error && <Notice>{error}</Notice>}
      {!products && !error && <Spinner />}
      {products && products.length === 0 && !error && <p className="py-6 text-center text-sm text-muted">{dq || status ? 'No products match this search.' : 'No products yet.'}</p>}
      <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {products?.map((p) => (
          <li key={p.sku} className="rounded-2xl border border-line bg-white p-4">
            <div className="flex gap-3">
              <div className="flex -space-x-2">{p.references.slice(0, 3).map((r) => <img key={r.id} src={r.thumb_url} alt="" title={r.source === 'review_crop' ? 'Shelf crop added during a review' : undefined} className="h-12 w-12 rounded-lg border-2 border-white object-cover" />)}{p.references.length === 0 && <span className="grid h-12 w-12 place-items-center rounded-lg bg-cream text-[10px] text-muted">no photo</span>}</div>
              <div className="min-w-0"><p className="truncate text-sm font-bold" title={p.name}>{p.name}</p>
                <p className="font-mono text-xs text-muted">{p.sku} · {p.reference_count} photo{p.reference_count === 1 ? '' : 's'}{!p.active && ' · inactive'}</p>
                <p className="mt-1"><IndexBadge status={p.status} /></p></div>
            </div>
            {p.status === 'pending' && <p className="mt-2 text-xs text-warn">No reference photos yet: this product cannot be recognised.</p>}
            {p.status === 'failed' && (write ? <RetryIndex p={p} onChanged={changed} /> : <p className="mt-2 text-xs text-danger">The matching service has not confirmed this product's photos yet. A manager can retry.</p>)}
            {p.source === 'openfoodfacts' && (
              <p className="mt-2 text-[10px] leading-snug text-muted" title={p.image_attribution ?? undefined}>
                Image: {p.image_license ?? 'see credits'}{p.image_attribution ? ` · ${p.image_attribution}` : ''}
                {safeHref(p.source_url) && <> · <a href={safeHref(p.source_url)} target="_blank" rel="noopener noreferrer" className="underline">Open Food Facts</a></>}
              </p>
            )}
            <div className="mt-3 flex items-center justify-between gap-2 text-xs">
              <label className="flex items-center gap-2 font-semibold">Expected
                {write && shelf ? <input type="number" min={0} defaultValue={qty(p.sku)} key={`${shelf}-${p.sku}-${qty(p.sku)}`} aria-label={`Expected quantity for ${p.name}`} className="w-20 rounded-lg border border-line-strong px-2 py-1.5 font-mono"
                  onBlur={(e) => { const v = e.target.value; if (v !== '' && Number(v) !== qty(p.sku)) stock.set(shelf, p.sku, Number(v)).then((r) => setExpected((c) => [...c.filter((x) => x.sku !== p.sku), r])).catch((x) => setError(errMsg(x))); }} />
                  : <span className="font-mono">{qty(p.sku) === '' ? '—' : qty(p.sku)}</span>}
              </label>
              {write && p.active && <AddPhotos p={p} onChanged={changed} />}
            </div>
            {write && p.active && p.status === 'ready' && <TestRecognition p={p} />}
          </li>
        ))}
      </ul>
      {more && <div className="text-center"><button type="button" className={btnSecondary} disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? 'Loading…' : 'Load more'}</button></div>}
      {products && products.length > 0 && <p className="text-center text-xs text-muted" role="status">Showing {products.length} product{products.length === 1 ? '' : 's'}{more ? '' : ' (all)'}.</p>}
      <p className="border-t border-line pt-3 text-xs text-muted">Product photos and data imported from Open Food Facts keep their licence. See <button type="button" onClick={() => go('credits')} className="font-semibold text-brand underline">Image credits</button>.</p>
    </div>
  );
}
