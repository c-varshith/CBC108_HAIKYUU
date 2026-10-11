import { ArrowLeft, Download, Pin, RotateCcw, Send, Undo2 } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { catalog, scans, watchScan } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import type { DetState, PatchAction, Product, Progress, ScanView } from '../../lib/contract';
import { detectionCounts, groupUndo } from '../../lib/selectors';
import { cn } from '../../utils/cn';
import { btnPrimary, btnSecondary } from '../ui';
import { Crop, Notice, STATE_STYLE, ScanCanvas, Spinner, StateChip, StatusBadge, STATUS_HINT, errMsg, fmtDate, go } from './shared';

const STEPS = ['Detecting', 'Matching', 'Counting'];

/** Processing: real server progress over SSE (poll fallback), then flips to the review view by itself. */
function Processing({ view, reload }: { view: ScanView; reload: () => void }) {
  const { token } = useAuth();
  const [p, setP] = useState<Progress | null>(null);
  useEffect(() => {
    let stop: (() => void) | null = null;
    let poll: number | null = null;
    let polling = false;
    let alive = true;
    const fallback = () => {
      if (poll !== null) return;
      poll = window.setInterval(() => {
        if (polling) return;
        polling = true;
        scans.progress(view.scan_id)
          .then((progress) => {
            if (!alive) return;
            setP(progress);
            if (progress.status !== 'UPLOADED' && progress.status !== 'PROCESSING') reload();
          })
          .catch(() => { if (alive) reload(); })
          .finally(() => { polling = false; });
      }, 1500);
    };
    token().then((t) => {
      if (!alive) return;
      if (!t) return fallback();
      stop = watchScan(view.scan_id, t, (x) => { setP(x); if (x.status !== 'UPLOADED' && x.status !== 'PROCESSING') reload(); }, () => { reload(); fallback(); });
    });
    return () => { alive = false; stop?.(); if (poll !== null) window.clearInterval(poll); };
  }, [view.scan_id, token, reload]);

  const step = (p?.step ?? 'Detecting').toLowerCase();
  const idx = Math.max(0, STEPS.findIndex((s) => step.startsWith(s.toLowerCase().slice(0, 4))));
  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <ScanCanvas src={view.image.display_url} width={view.image.width} height={view.image.height} detections={[]} selectedId={null} filter="ALL" showLabels={false} onSelect={() => undefined} scanning />
      <div className="rounded-2xl border border-line bg-white p-5">
        <ol className="flex gap-2" aria-label="Processing steps">
          {STEPS.map((s, i) => (
            <li key={s} className={cn('flex-1 rounded-lg px-3 py-2 text-center text-xs font-bold', i <= idx ? 'bg-ink text-white' : 'bg-cream text-muted')} aria-current={i === idx ? 'step' : undefined}>{s}</li>
          ))}
        </ol>
        <div className="mt-4 h-2 overflow-hidden rounded-full bg-cream"><div className="h-full bg-brand transition-all" style={{ width: `${Math.max(5, p?.pct ?? 5)}%` }} /></div>
        <p className="mt-3 text-sm text-muted" role="status">{p?.found != null ? `${p.found} items found so far. ` : ''}You can leave this page: it keeps running and shows up in your scans.</p>
      </div>
    </div>
  );
}

export function ScanDetail({ id }: { id: string }) {
  const { can } = useAuth();
  const [view, setView] = useState<ScanView | null>(null);
  const [products, setProducts] = useState<Product[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState<DetState | 'ALL'>('ALL');
  const [labels, setLabels] = useState(true);
  const [sel, setSel] = useState<string | null>(null);
  const [multiSel, setMultiSel] = useState<string[]>([]);
  const [bulkSku, setBulkSku] = useState('');
  const [tab, setTab] = useState<'review' | 'counts' | 'activity'>('counts');
  const [sheet, setSheet] = useState<null | 'submit' | 'return' | 'reopen'>(null);
  const [note, setNote] = useState('');
  const [pins, setPins] = useState<string[]>([]);
  const [comment, setComment] = useState('');
  const [search, setSearch] = useState('');
  const history = useRef<{ ids: string[]; prev: { id: string; state: DetState; sku: string | null; deleted?: boolean; accepted?: boolean }[] }[]>([]);

  const reload = useCallback(() => scans.get(id).then(setView).catch((e) => setError(errMsg(e))), [id]);
  useEffect(() => { reload(); catalog.products(false, { slim: true }).then(setProducts).catch(() => undefined); }, [reload]);

  const dets = view?.detections ?? [];
  const live = useMemo(() => dets.filter((d) => !d.deleted), [dets]);
  const counts = useMemo(() => detectionCounts(dets), [dets]);
  const selected = dets.find((d) => d.id === sel) ?? null;
  // "unresolved" is decided by the server (totals.unresolved) so the Submit gate cannot drift from it
  const isOpen = (d: (typeof dets)[number]) => !d.deleted && !d.accepted_unknown && (d.state === 'CHECK' || d.state === 'UNKNOWN');
  const open = useMemo(() => dets.filter(isOpen), [dets]);
  const openBySku = useMemo(() => {
    const grouped = new Map<string, string[]>();
    for (const d of open) {
      const sku = d.assigned_sku ?? (d.state === 'CHECK' ? d.top_matches[0]?.sku : undefined);
      if (sku) grouped.set(sku, [...(grouped.get(sku) ?? []), d.id]);
    }
    return grouped;
  }, [open]);
  const unmatched = useMemo(() => live.filter((d) => !d.assigned_sku &&
    (d.accepted_unknown || d.state === 'UNKNOWN' || !d.top_matches[0])), [live]);
  // The summary also contains expected-only SKUs for reconciliation. Keep those out of the
  // estimate list: an item with no detected or reviewed boxes is not an AI estimate.
  const estimatedRows = useMemo(() => view?.summary.filter((row) => row.detected > 0 || row.reviewed > 0 || row.quantity_adjusted) ?? [], [view?.summary]);
  const skuEstimateTotal = useMemo(() => estimatedRows.reduce((sum, row) => sum + row.detected, 0), [estimatedRows]);
  const unknownNeedsReview = unmatched.some(isOpen);

  const canEdit = !!view?.permissions.can_edit;

  const apply = useCallback(async (ids: string[], action: PatchAction, sku?: string) => {
    if (!view || !ids.length) return;
    setBusy(true); setError(null);
    try {
      history.current.push({ ids, prev: view.detections.filter((d) => ids.includes(d.id)).map((d) => ({ id: d.id, state: d.state, sku: d.assigned_sku, deleted: d.deleted, accepted: d.accepted_unknown })) });
      setView(await scans.patch(view.scan_id, ids, action, sku));
    } catch (e) { history.current.pop(); setError(errMsg(e)); } finally { setBusy(false); }
  }, [view]);

  const next = useCallback((from: string | null, dir = 1) => {
    const list = view?.detections.filter(isOpen) ?? [];
    if (!list.length) return setSel(null);
    const i = list.findIndex((d) => d.id === from);
    setSel(list[(i + dir + list.length * 2) % list.length].id);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view]);

  // After a correction the box leaves the queue: move on automatically, like "Next uncertain".
  const act = useCallback(async (action: PatchAction, sku?: string) => {
    if (!sel) return;
    const cur = sel;
    await apply([cur], action, sku);
    setSel(null);
    setTimeout(() => next(cur, 1), 0);
  }, [sel, apply, next]);

  async function applySimilar() {
    if (!selected) return;
    const sku = selected.assigned_sku ?? selected.top_matches[0]?.sku;
    if (!sku) return;
    const ids = dets.filter((d) => !d.deleted && d.id !== selected.id &&
      (d.assigned_sku ?? d.top_matches[0]?.sku) === sku && d.state !== 'CORRECTED').map((d) => d.id);
    if (!ids.length) { setNotice('No other boxes share this suggestion.'); return; }
    await apply(ids, 'ASSIGN', sku);
    setNotice(`Applied ${sku} to ${ids.length} similar boxes.`);
    setMultiSel([]);
  }

  async function applyBulk() {
    const ids = multiSel.filter((id) => id !== selected?.id || multiSel.length > 1);
    if (!bulkSku || !ids.length) return;
    await apply(ids, 'ASSIGN', bulkSku);
    setNotice(`Assigned ${bulkSku} to ${ids.length} boxes.`);
    setMultiSel([]);
    setBulkSku('');
  }

  // True step-back: re-apply each box's previous state (assigned SKU, not-a-product, accepted unknown, or untouched).
  const undo = useCallback(async () => {
    const last = history.current.pop();
    if (!last || !view) return;
    setBusy(true);
    try {
      const groups = groupUndo(last.prev);
      let latest: ScanView | null = null;
      for (const g of groups.values()) latest = await scans.patch(view.scan_id, g.ids, g.action, g.sku);
      if (latest) setView(latest);
    } catch (e) { setError(errMsg(e)); } finally { setBusy(false); }
  }, [view]);

  useEffect(() => {
    if (!canEdit) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.closest('input, textarea, select, [contenteditable]') || e.ctrlKey && e.key !== 'z' || e.metaKey && e.key !== 'z') return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); undo(); return; }
      if (!selected) { if (e.key === 'n' || e.key === 'ArrowRight') next(null, 1); return; }
      const top = selected.top_matches;
      if (e.key === 'Enter' && top[0]) act('CONFIRM');
      else if (['1', '2', '3'].includes(e.key) && top[Number(e.key) - 1]) act('ASSIGN', top[Number(e.key) - 1].sku);
      else if (e.key === 'Delete' || e.key === 'Backspace') act('NOT_A_PRODUCT');
      else if (e.key.toLowerCase() === 'u') act('UNKNOWN');
      else if (e.key.toLowerCase() === 'n' || e.key === 'ArrowRight') next(selected.id, 1);
      else if (e.key.toLowerCase() === 'p' || e.key === 'ArrowLeft') next(selected.id, -1);
      else if (e.key === 'Escape') setSel(null);
      else if (e.key.toLowerCase() === 'l') setLabels((v) => !v);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [canEdit, selected, act, next, undo]);

  async function run(fn: () => Promise<ScanView | unknown>, done?: string) {
    setBusy(true); setError(null); setNotice(null);
    try { const r = await fn(); if (r && typeof r === 'object' && 'scan_id' in r && 'detections' in r) setView(r as ScanView); else await reload(); if (done) setNotice(done); setSheet(null); setNote(''); setPins([]); }
    catch (e) { setError(errMsg(e)); } finally { setBusy(false); }
  }

  async function saveCount(sku: string, quantity: number | null) {
    await run(() => scans.setCount(id, sku, quantity), quantity === null ? 'Count restored to the reviewed box total.' : 'Count updated. It will be used after approval.');
  }

  if (!view) return error ? <Notice>{error}</Notice> : <Spinner />;

  const back = <button type="button" onClick={() => go('scans')} className="mb-3 inline-flex items-center gap-1.5 text-xs font-semibold text-muted hover:text-ink"><ArrowLeft className="h-3.5 w-3.5" />All scans</button>;
  if (view.status === 'UPLOADED' || view.status === 'PROCESSING') return <div>{back}<Processing view={view} reload={reload} /></div>;
  if (view.status === 'FAILED') return (
    <div className="mx-auto max-w-lg space-y-4">{back}
      <Notice>Processing failed. {view.timeline.at(-1)?.note ?? ''}</Notice>
      {can('scan.capture') && <button type="button" className={btnPrimary} disabled={busy} onClick={() => run(async () => { await scans.retry(id); await reload(); })}>Retry processing</button>}
    </div>
  );

  const p = view.permissions;
  const unresolved = view.totals.unresolved;
  const pinnedFromManager = view.status === 'RETURNED' || view.pinned_detection_ids.length > 0 ? view.pinned_detection_ids : [];
  const candidates = selected ? selected.top_matches : [];
  const filtered = products.filter((x) => `${x.sku} ${x.name} ${x.brand ?? ''} ${x.barcode ?? ''}`.toLowerCase().includes(search.trim().toLowerCase())).slice(0, 8);
  const shortfalls = view.summary.filter((r) => r.status === 'SHORTFALL' && !r.within_tolerance);

  return (
    <div>
      {back}
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2"><StatusBadge status={view.status} /><span className="text-xs text-muted">by {view.created_by.name}</span></div>
          <p className="mt-1.5 text-sm text-muted">{STATUS_HINT[view.status]}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          {canEdit && history.current.length > 0 && <button type="button" className={btnSecondary} onClick={undo} disabled={busy}><Undo2 className="h-4 w-4" />Undo last fix</button>}
          {can('reports.export') && <button type="button" className={btnSecondary} onClick={() => scans.export(id, 'csv').catch((e) => setError(errMsg(e)))}><Download className="h-4 w-4" />CSV</button>}
          {can('reports.export') && <button type="button" className={btnSecondary} onClick={() => scans.export(id, 'pdf').catch((e) => setError(errMsg(e)))}>PDF</button>}
          {p.can_submit && <button type="button" className={btnPrimary} disabled={busy || unresolved > 0} onClick={() => setSheet('submit')} title={unresolved > 0 ? `Review ${unresolved} flagged boxes before submitting` : undefined}><Send className="h-4 w-4" />Submit reviewed count</button>}
          {p.can_approve && <button type="button" className={btnPrimary} disabled={busy || unresolved > 0} onClick={() => run(() => scans.approve(id), 'Scan approved and locked.')}>Approve</button>}
          {p.can_approve && <button type="button" className={btnSecondary} disabled={busy} onClick={() => setSheet('return')}>Return…</button>}
          {p.can_reopen && <button type="button" className={btnSecondary} disabled={busy} onClick={() => setSheet('reopen')}><RotateCcw className="h-4 w-4" />Reopen</button>}
        </div>
      </div>

      {view.approval?.decision === 'RETURNED' && view.status === 'RETURNED' && (
        <div className="mb-4"><Notice tone="warn"><b>Returned by {view.approval.decided_by.name}:</b> {view.approval.note || 'No note.'} {pinnedFromManager.length > 0 && <>Pinned boxes are outlined in orange.</>}</Notice></div>
      )}
      {error && <div className="mb-4"><Notice>{error}</Notice></div>}
      {notice && <div className="mb-4"><Notice tone="brand">{notice}</Notice></div>}
      {!canEdit && view.status === 'APPROVED' && <div className="mb-4"><Notice tone="brand">This scan is approved and locked (read-only).</Notice></div>}
      {view.status === 'APPROVED' && <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-brand/25 bg-brand-mist px-4 py-3 text-sm">
        <span>The approved product counts are reflected in Inventory.</span>
        <button type="button" className={btnSecondary} onClick={() => go('inventory')}>View inventory</button>
      </div>}

      <div className="space-y-4">
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filter boxes">
            {(['ALL', 'SURE', 'CHECK', 'UNKNOWN', 'CORRECTED'] as const).map((f) => (
              <button key={f} type="button" aria-pressed={filter === f} onClick={() => setFilter(f)}
                className={cn('min-h-9 rounded-full border px-3 text-xs font-bold', filter === f ? 'border-ink bg-ink text-white' : 'border-line-strong bg-white text-muted')}>
                {f === 'ALL' ? 'All' : STATE_STYLE[f].label} <span className="font-mono tabular-nums">{counts[f]}</span>
              </button>
            ))}
            <label className="ml-auto flex items-center gap-1.5 text-xs font-semibold"><input type="checkbox" checked={labels} onChange={(e) => setLabels(e.target.checked)} />Labels</label>
          </div>
          {canEdit && multiSel.length > 1 && <div className="flex flex-wrap items-end gap-2 rounded-xl border border-cyan-700/20 bg-cyan-50 p-2.5">
            <span className="mr-auto self-center text-xs font-bold">{multiSel.length} boxes selected</span>
            <label className="text-[10px] font-semibold">Assign all
              <select value={bulkSku} onChange={(e) => setBulkSku(e.target.value)} aria-label="Bulk assign product" className="ml-2 rounded-lg border border-line-strong bg-white px-2 py-2 text-xs">
                <option value="">Choose product…</option>{products.map((prod) => <option key={prod.sku} value={prod.sku}>{prod.name} ({prod.sku})</option>)}
              </select>
            </label>
            <button type="button" disabled={busy || !bulkSku} className={btnPrimary} onClick={applyBulk}>Apply to selected</button>
            <button type="button" className={btnSecondary} onClick={() => setMultiSel([])}>Clear</button>
          </div>}
          <ScanCanvas src={view.image.display_url} width={view.image.width} height={view.image.height} detections={dets} selectedId={sel} selectedIds={multiSel} onMultiSelect={setMultiSel} pinned={[...pinnedFromManager, ...pins]} filter={filter} showLabels={labels && live.length < 80}
            onSelect={(d) => { setSel(d); setTab('review'); }} />
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {[['Total', view.totals.detected], ['Needs review', unresolved], ['Unknown', view.totals.unknown], ['Variance flags', shortfalls.length]].map(([l, v]) => (
              <div key={l as string} className="rounded-xl border border-line bg-white px-3 py-2.5"><p className="text-[10px] font-bold uppercase tracking-wider text-muted">{l}</p><p className="font-mono text-xl font-bold tabular-nums" aria-live="polite">{v}</p></div>
            ))}
          </div>
        </div>

        <div className="rounded-2xl border border-line bg-white">
          <div role="tablist" className="flex border-b border-line">
            {(['review', 'counts', 'activity'] as const).map((t) => (
              <button key={t} role="tab" aria-selected={tab === t} type="button" onClick={() => setTab(t)} className={cn('flex-1 px-3 py-3 text-xs font-bold capitalize', tab === t ? 'border-b-2 border-brand text-ink' : 'text-muted')}>
                {t === 'review' ? `Review boxes (${open.length})` : t === 'counts' ? 'Item estimates' : 'Activity'}
              </button>
            ))}
          </div>

          {tab === 'review' && (
            <div className="space-y-3 p-4">
              {!selected && (
                <div className="py-6 text-center">
                  <p className="text-sm font-semibold">{open.length === 0 ? 'Every box is resolved.' : 'Tap a yellow or red box to review it.'}</p>
                  {open.length > 0 && canEdit && <button type="button" className={`${btnPrimary} mt-3`} onClick={() => next(null, 1)}>Next uncertain</button>}
                </div>
              )}
              {selected && (
                <>
                  <div className="flex items-center gap-3">
                    <Crop src={view.image.display_url} width={view.image.width} height={view.image.height} bbox={selected.bbox} className="h-24 w-24 shrink-0" />
                    <div><StateChip state={selected.state} /><p className="mt-1.5 font-mono text-xs text-muted">detector {Math.round(selected.det_confidence * 100)}%</p>
                      {selected.assigned_sku && <p className="mt-1 text-xs">Assigned: <b className="font-mono">{selected.assigned_sku}</b></p>}
                      {selected.accepted_unknown && <p className="mt-1 text-xs">Accepted as Unknown</p>}</div>
                  </div>
                  {canEdit ? (
                    <>
                      {selected.state === 'UNKNOWN' && !selected.assigned_sku && (
                        <p className="rounded-xl border border-line px-3 py-2.5 text-xs text-muted">No match passed the confidence threshold. These are the closest catalog products; choose one if it is correct, or search the catalog.</p>
                      )}
                      {candidates.length > 0 && (
                      <ul className="space-y-1.5" aria-label="Suggested products">
                        {candidates.map((m, i) => (
                          <li key={m.sku}><button type="button" disabled={busy} onClick={() => act(selected.state === 'CHECK' && i === 0 ? 'CONFIRM' : 'ASSIGN', m.sku)} className="flex min-h-11 w-full items-center justify-between rounded-xl border border-line-strong px-3 text-left text-sm hover:border-brand">
                            <span><kbd className="mr-2 rounded bg-cream px-1.5 font-mono text-[10px]">{i + 1}</kbd>{m.name ?? m.sku}<span className="ml-2 font-mono text-[10px] text-muted">{m.sku}</span></span><span className="font-mono text-xs text-muted">Similarity {m.score.toFixed(2)}</span></button></li>
                        ))}
                      </ul>
                      )}
                      {candidates.length === 0 && <p className="text-xs text-muted">No suggested catalog products. Search by product name, brand, barcode, or SKU.</p>}
                      {selected.state !== 'UNKNOWN' && selected.top_matches[0] && <button type="button" disabled={busy} className={`${btnSecondary} w-full`} onClick={applySimilar}>
                        Apply {selected.assigned_sku ?? selected.top_matches[0].sku} to similar boxes
                      </button>}
                      <div>
                        <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search the full catalog…" aria-label="Search catalog" className="w-full rounded-xl border border-line-strong px-3 py-2.5 text-sm" />
                        {search && <ul className="mt-1 max-h-44 overflow-auto rounded-xl border border-line">{filtered.map((x) => <li key={x.sku}><button type="button" className="w-full px-3 py-2.5 text-left text-sm hover:bg-cream" onClick={() => { setSearch(''); act('ASSIGN', x.sku); }}>{x.name} <span className="font-mono text-xs text-muted">{x.sku}</span></button></li>)}{filtered.length === 0 && <li className="px-3 py-2 text-xs text-muted">No product matches.</li>}</ul>}
                      </div>
                      <div className="grid grid-cols-2 gap-2">
                        <button type="button" disabled={busy} className={btnSecondary} onClick={() => act('NOT_A_PRODUCT')}>Not a product</button>
                        <button type="button" disabled={busy} className={btnSecondary} onClick={() => act('UNKNOWN')}>Accept Unknown</button>
                        <button type="button" disabled={busy} className={btnSecondary} onClick={() => apply([selected.id], 'RESET')}>Reset</button>
                        <button type="button" className={btnSecondary} onClick={() => next(selected.id, 1)}>Skip</button>
                      </div>
                      {can('catalog.write') ? <a href="#/app/catalog" className="block text-center text-xs font-semibold text-brand underline">Register as new product</a>
                        : can('catalog.request') && <RequestProduct />}
                      <p className="hidden text-[11px] text-muted md:block">Keys: Enter confirm · 1/2/3 pick · U unknown · Del not a product · N/P next/prev · Ctrl+Z undo</p>
                    </>
                  ) : <p className="text-xs text-muted">Read-only.</p>}
                  {selected.state === 'CORRECTED' && selected.assigned_sku && can('catalog.write') && (
                    <CropAsReference key={`${selected.id}-${selected.assigned_sku}`} sku={selected.assigned_sku} scanId={view.scan_id} detectionId={selected.id} />
                  )}
                  {p.can_approve && view.status === 'SUBMITTED' && (
                    <button type="button" className={btnSecondary} onClick={() => setPins((c) => (c.includes(selected.id) ? c.filter((x) => x !== selected.id) : [...c, selected.id]))}><Pin className="h-4 w-4" />{pins.includes(selected.id) ? 'Unpin from return' : 'Pin to return note'}</button>
                  )}
                </>
              )}
            </div>
          )}

          {tab === 'counts' && (
            <div className="space-y-4 p-4 sm:p-5">
              <div className="rounded-2xl border border-brand/20 bg-brand-mist p-4 shadow-sm sm:p-5">
                <p className="text-sm font-semibold tracking-[-0.01em] text-ink">AI quantity estimates <span className="font-normal text-muted">· human approval required</span></p>
                <p className="mt-1.5 text-sm leading-relaxed text-muted">
                  Product names come from the catalog. For uncertain matches, choose the correct catalog suggestion or search by name, brand, barcode, or SKU. After every flagged box is resolved, submit the reviewed count; Inventory updates when an Owner or Manager approves it.
                </p>
                {canEdit && open.length > 0 && <button type="button" className={`${btnPrimary} mt-4 w-full`} onClick={() => { setTab('review'); setSel(open[0].id); }}>
                  Review flagged boxes ({open.length})
                </button>}
                {canEdit && open.length === 0 && <p className="mt-3 text-sm font-semibold text-brand">All flagged boxes are resolved. Submit the reviewed count for approval.</p>}
                {p.can_submit && <div className="mt-4 rounded-xl border border-line bg-white p-3">
                  <p className="text-xs leading-relaxed text-muted">{unresolved > 0 ? `Resolve the ${unresolved} flagged boxes to enable submission.` : 'Submit these reviewed product counts. Inventory updates after an Owner or Manager approves the scan.'}</p>
                  <button type="button" disabled={busy || unresolved > 0} title={unresolved > 0 ? `${unresolved} flagged boxes still need a decision` : undefined} className={`${btnPrimary} mt-3 w-full`} onClick={() => setSheet('submit')}>
                    <Send className="h-4 w-4" />Submit reviewed count
                  </button>
                </div>}
                {view.status === 'SUBMITTED' && <p className="mt-3 text-sm font-semibold text-brand">{p.can_approve ? 'Review these estimates, then approve or return the scan to update Inventory.' : 'Submitted. Inventory will update after an authorized approver approves this scan.'}</p>}
              </div>
              {canEdit && <ManualProductCount products={products.filter((product) => !estimatedRows.some((row) => row.sku === product.sku))}
                disabled={busy} onSave={saveCount} />}
              <div className="grid grid-cols-3 gap-2 text-center">
                {[
                  { label: 'Visible items', value: view.totals.detected },
                  { label: 'SKU estimates', value: skuEstimateTotal },
                  { label: 'Unknown', value: unmatched.length },
                ].map(({ label, value }) => <div key={label} className="min-w-0 rounded-xl border border-line bg-white px-2 py-3 shadow-sm sm:py-3.5">
                  <p className="truncate text-[10px] font-semibold uppercase tracking-[0.08em] text-muted sm:text-[11px]">{label}</p>
                  <p className="mt-1 font-mono text-xl font-bold tabular-nums tracking-tight text-ink">{value}</p>
                </div>)}
              </div>
              <div className="space-y-3">
                {unmatched.length > 0 && <article className="min-w-0 rounded-2xl border border-warn/35 bg-white p-4 shadow-sm sm:p-5">
                  <div className="flex min-w-0 items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="break-words text-sm font-semibold tracking-[-0.01em]">Unknown / no catalog match</p>
                      <p className="mt-1 text-xs leading-relaxed text-muted sm:text-[13px]">Detected packages without a reliable SKU match</p>
                    </div>
                    <span className={cn('shrink-0 rounded-full px-2.5 py-1 text-[10px] font-bold uppercase tracking-wide', unknownNeedsReview ? 'bg-warn-soft text-warn' : 'bg-brand-soft text-brand-deep')}>
                      {unknownNeedsReview ? 'Needs review' : 'Reviewed'}
                    </span>
                  </div>
                  <dl className="mt-4 grid grid-cols-2 gap-x-5 gap-y-2">
                    <div className="flex min-w-0 items-baseline justify-between gap-2 border-t border-line pt-2"><dt className="truncate text-xs text-muted">Est. qty</dt><dd className="font-mono text-sm font-bold tabular-nums text-ink">{unmatched.length}</dd></div>
                    <div className="flex min-w-0 items-baseline justify-between gap-2 border-t border-line pt-2"><dt className="truncate text-xs text-muted">Resolved</dt><dd className="font-mono text-sm font-semibold tabular-nums text-ink">{unmatched.filter((d) => d.accepted_unknown).length}</dd></div>
                  </dl>
                  {canEdit && unknownNeedsReview && <button type="button" className="mt-3 max-w-full break-words text-left text-xs font-semibold text-brand underline decoration-brand/40 underline-offset-4 hover:decoration-brand" onClick={() => { setTab('review'); setSel(unmatched.find(isOpen)?.id ?? unmatched[0]?.id ?? null); }}>Review unknown items</button>}
                </article>}
                {[...estimatedRows].sort((a, b) => a.variance - b.variance).map((r) => {
                  const flagged = openBySku.get(r.sku) ?? [];
                  return <article key={r.sku} className={cn('min-w-0 rounded-2xl border bg-white p-4 shadow-sm sm:p-5', r.status === 'SHORTFALL' ? 'border-danger/20' : r.status === 'SURPLUS' ? 'border-warn/25' : 'border-line')}>
                    <div className="flex min-w-0 items-start justify-between gap-2">
                      <div className="min-w-0">
                        <p className="break-words text-sm font-semibold leading-snug tracking-[-0.01em]">{r.name}</p>
                        <p className="mt-1 break-all font-mono text-[11px] text-muted">{r.sku}</p>
                      </div>
                      <span className={cn('shrink-0 rounded-full px-2.5 py-1 text-[10px] font-bold uppercase tracking-wide', r.status === 'SHORTFALL' ? 'bg-danger-soft text-danger' : r.status === 'SURPLUS' ? 'bg-warn-soft text-warn' : 'bg-brand-soft text-brand-deep')}>
                        {r.status === 'SHORTFALL' ? 'Short' : r.status === 'SURPLUS' ? 'Over' : 'OK'}
                      </span>
                    </div>
                    <dl className="mt-4 grid grid-cols-2 gap-x-5 gap-y-2.5">
                      <div className="flex min-w-0 items-baseline justify-between gap-2 border-t border-line pt-2">
                        <dt className="truncate text-xs text-muted">AI est. qty</dt>
                        <dd className="shrink-0 font-mono text-sm font-semibold tabular-nums text-ink">{r.detected}</dd>
                      </div>
                      <div className="flex min-w-0 items-start justify-between gap-2 border-t border-line pt-2">
                        <dt className="pt-1 truncate text-xs text-muted">Counted qty</dt>
                        <dd className="shrink-0"><CountEditor quantity={r.reviewed} aiQuantity={r.ai_reviewed ?? r.reviewed}
                          adjusted={r.quantity_adjusted ?? false} disabled={!canEdit || busy} onSave={(quantity) => saveCount(r.sku, quantity)} /></dd>
                      </div>
                      {[['Expected', r.expected], ['Variance', `${r.variance > 0 ? '+' : ''}${r.variance}`]].map(([label, value]) => (
                        <div key={label} className="flex min-w-0 items-baseline justify-between gap-2 border-t border-line pt-2">
                          <dt className="truncate text-xs text-muted">{label}</dt>
                          <dd className="shrink-0 font-mono text-sm font-semibold tabular-nums text-ink">{value}</dd>
                        </div>
                      ))}
                    </dl>
                    {flagged.length > 0 && canEdit && <button type="button" className="mt-3 max-w-full break-words text-left text-xs font-semibold text-brand underline decoration-brand/40 underline-offset-4 hover:decoration-brand" onClick={() => { setTab('review'); setSel(flagged[0]); }}>Review {flagged.length} flagged {flagged.length === 1 ? 'box' : 'boxes'}</button>}
                  </article>;
                })}
                {estimatedRows.length === 0 && unmatched.length === 0 && <p className="p-6 text-center text-xs text-muted">No catalog-matched items were detected. Unknown detections appear above.</p>}
              </div>
            </div>
          )}

          {tab === 'activity' && (
            <div className="space-y-3 p-4">
              <ol className="space-y-3">
                {view.timeline.map((t, i) => (
                  <li key={i} className="border-l-2 border-line pl-3 text-xs"><p className="font-bold">{t.event.replace('_', ' ')} <span className="font-normal text-muted">· {t.actor}</span></p>{t.note && <p className="mt-0.5">{t.note}</p>}<p className="text-[10px] text-muted">{fmtDate(t.at)}</p></li>
                ))}
              </ol>
              {can('scan.review') && (
                <form onSubmit={(e) => { e.preventDefault(); if (comment.trim()) run(() => scans.comment(id, comment.trim())).then(() => setComment('')); }} className="flex gap-2">
                  <input value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Add a comment" aria-label="Add a comment" className="min-w-0 flex-1 rounded-xl border border-line-strong px-3 py-2 text-sm" />
                  <button type="submit" disabled={busy} className={btnSecondary}>Post</button>
                </form>
              )}
            </div>
          )}
        </div>
      </div>

      {sheet && (
        <div role="dialog" aria-modal="true" aria-label={sheet} className="fixed inset-0 z-50 flex items-end justify-center bg-ink/50 p-0 md:items-center md:p-4">
          <div className="w-full max-w-md rounded-t-2xl bg-white p-5 shadow-pop md:rounded-2xl">
            <h2 className="text-lg font-bold">{sheet === 'submit' ? 'Submit for approval' : sheet === 'return' ? 'Return to staff' : 'Reopen scan'}</h2>
            {sheet === 'submit' && <><ul className="mt-2 space-y-1 text-sm text-muted"><li>✓ All flagged boxes resolved</li><li>{shortfalls.length ? `⚠ ${shortfalls.length} SKU${shortfalls.length > 1 ? 's' : ''} short of expected` : '✓ No shortfalls beyond tolerance'}</li></ul><p className="mt-3 text-xs leading-relaxed text-muted">An Owner or Manager must approve this count before it becomes the latest inventory count for this shelf.</p></>}
            {sheet === 'return' && <p className="mt-2 text-xs text-muted">{pins.length} box{pins.length === 1 ? '' : 'es'} pinned. Select a box and use “Pin to return note” to point at specific items.</p>}
            <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={3} placeholder={sheet === 'submit' ? 'Optional note' : sheet === 'return' ? 'What should be fixed?' : 'Reason (required)'} className="mt-3 w-full rounded-xl border border-line-strong p-3 text-sm" />
            <div className="mt-4 flex justify-end gap-2">
              <button type="button" className={btnSecondary} onClick={() => setSheet(null)}>Cancel</button>
              <button type="button" disabled={busy || (sheet !== 'submit' && !note.trim())} className={btnPrimary}
                onClick={() => run(() => sheet === 'submit' ? scans.submit(id, note) : sheet === 'return' ? scans.return(id, note.trim(), pins) : scans.reopen(id, note.trim()), sheet === 'submit' ? 'Submitted. A manager will review it.' : sheet === 'return' ? 'Returned to staff.' : 'Scan reopened.')}>Confirm</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/** A reviewer fixed this box to a product: keep the shelf crop as an extra reference photo. Shelf crops close the gap between catalog packshots and real shelves. */
function CropAsReference({ sku, scanId, detectionId }: { sku: string; scanId: string; detectionId: string }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: 'brand' | 'danger'; text: string } | null>(null);
  async function add() {
    setBusy(true); setMsg(null);
    try {
      const p = await catalog.addReferenceFromDetection(sku, scanId, detectionId);
      setMsg({ tone: 'brand', text: `Added. ${p.name} now has ${p.reference_count} reference photo${p.reference_count === 1 ? '' : 's'} and is searchable.` });
    } catch (e) { setMsg({ tone: 'danger', text: errMsg(e) }); } finally { setBusy(false); }
  }
  return (
    <div className="space-y-1.5 rounded-xl border border-line p-3">
      <button type="button" disabled={busy} className={`${btnSecondary} w-full`} onClick={() => void add()}>{busy ? 'Adding…' : 'Add this crop as a reference photo'}</button>
      <p className="text-[11px] text-muted">Saves this box as another photo of <span className="font-mono">{sku}</span>, so the next scan recognises it on a real shelf. No retraining.</p>
      {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
    </div>
  );
}

function CountEditor({ quantity, aiQuantity, adjusted, disabled, onSave }: {
  quantity: number; aiQuantity: number; adjusted: boolean; disabled: boolean; onSave: (quantity: number | null) => void;
}) {
  const [value, setValue] = useState(String(quantity));
  useEffect(() => setValue(String(quantity)), [quantity]);
  const parsed = /^\d+$/.test(value) ? Number(value) : Number.NaN;
  const valid = Number.isSafeInteger(parsed) && parsed >= 0 && parsed <= 1_000_000;
  const dirty = valid && parsed !== quantity;
  return (
    <div className="flex flex-col items-end gap-1.5">
      <div className="flex items-center gap-1.5">
        <input type="number" min={0} max={1_000_000} step={1} value={value} disabled={disabled}
          onChange={(e) => setValue(e.target.value)} aria-label="Counted quantity" className="w-20 rounded-lg border border-line-strong px-2 py-1 text-right font-mono text-sm tabular-nums" />
        <button type="button" className="rounded-lg bg-ink px-2.5 py-1.5 text-xs font-semibold text-white disabled:opacity-40"
          disabled={disabled || !dirty} onClick={() => onSave(parsed)}>Save</button>
      </div>
      {adjusted && <button type="button" disabled={disabled} onClick={() => onSave(null)} className="text-[10px] font-semibold text-brand underline disabled:opacity-40">
        Reset to reviewed total ({aiQuantity})
      </button>}
    </div>
  );
}

function ManualProductCount({ products, disabled, onSave }: {
  products: Product[]; disabled: boolean; onSave: (sku: string, quantity: number | null) => void;
}) {
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<Product | null>(null);
  const [quantity, setQuantity] = useState('0');
  const matches = search.trim() ? products.filter((p) => `${p.name} ${p.sku} ${p.brand ?? ''} ${p.barcode ?? ''}`
    .toLowerCase().includes(search.trim().toLowerCase())).slice(0, 8) : [];
  const parsed = /^\d+$/.test(quantity) ? Number(quantity) : Number.NaN;
  const valid = Number.isSafeInteger(parsed) && parsed >= 0 && parsed <= 1_000_000;
  return (
    <div className="rounded-xl border border-line bg-white p-3">
      <p className="text-xs font-semibold text-ink">Add a missed catalog product</p>
      <p className="mt-1 text-[11px] text-muted">Use this when a product wasn’t detected in the photo. Its AI estimate stays 0; the counted quantity is saved for Inventory after approval.</p>
      {!selected ? <div className="mt-2">
        <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search name, brand, barcode, or SKU" aria-label="Search catalog to add a missed product" className="w-full rounded-xl border border-line-strong px-3 py-2 text-sm" />
        {search && <ul className="mt-1 max-h-40 overflow-auto rounded-xl border border-line">{matches.map((p) => <li key={p.sku}>
          <button type="button" className="w-full px-3 py-2 text-left text-sm hover:bg-cream" onClick={() => { setSelected(p); setSearch(''); setQuantity('0'); }}>
            {p.name} <span className="font-mono text-xs text-muted">{p.sku}</span>
          </button>
        </li>)}{matches.length === 0 && <li className="px-3 py-2 text-xs text-muted">No active catalog product matches.</li>}</ul>}
      </div> : <div className="mt-2 flex flex-wrap items-center gap-2">
        <span className="min-w-0 flex-1 text-sm font-semibold">{selected.name} <span className="font-mono text-xs text-muted">{selected.sku}</span></span>
        <label className="flex items-center gap-1.5 text-xs text-muted">Count
          <input type="number" min={0} max={1_000_000} step={1} value={quantity} disabled={disabled} onChange={(e) => setQuantity(e.target.value)} aria-label={`Count for ${selected.name}`} className="w-20 rounded-lg border border-line-strong px-2 py-1 text-right font-mono text-sm text-ink" />
        </label>
        <button type="button" disabled={disabled || !valid} className={btnPrimary} onClick={() => { onSave(selected.sku, parsed); setSelected(null); setQuantity('0'); }}>Save count</button>
        <button type="button" disabled={disabled} className={btnSecondary} onClick={() => setSelected(null)}>Cancel</button>
      </div>}
    </div>
  );
}

function RequestProduct() {
  const [name, setName] = useState('');
  const [msg, setMsg] = useState<string | null>(null);
  return (
    <form className="space-y-1.5" onSubmit={async (e) => { e.preventDefault(); try { await catalog.request(name.trim()); setMsg('Request sent to a manager.'); setName(''); } catch (x) { setMsg(errMsg(x)); } }}>
      <div className="flex gap-2"><input value={name} onChange={(e) => setName(e.target.value)} placeholder="Request a new product (name)" aria-label="New product name" className="min-w-0 flex-1 rounded-xl border border-line-strong px-3 py-2 text-sm" /><button type="submit" disabled={!name.trim()} className={btnSecondary}>Request</button></div>
      {msg && <p className="text-xs text-muted" role="status">{msg}</p>}
    </form>
  );
}
