import { ArrowLeft, Download, Pin, RotateCcw, Send, Undo2 } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
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
    let alive = true;
    const fallback = () => { if (poll === null) poll = window.setInterval(reload, 2000); };
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
  const [panePct, setPanePct] = useState(65);
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
      const sku = d.assigned_sku ?? d.top_matches[0]?.sku;
      if (sku) grouped.set(sku, [...(grouped.get(sku) ?? []), d.id]);
    }
    return grouped;
  }, [open]);
  const unmatched = useMemo(() => live.filter((d) => !d.assigned_sku && !d.top_matches[0]), [live]);

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
  const filtered = products.filter((x) => (x.sku + x.name).toLowerCase().includes(search.toLowerCase())).slice(0, 8);
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
          {p.can_submit && <button type="button" className={btnPrimary} disabled={busy || unresolved > 0} onClick={() => setSheet('submit')} title={unresolved > 0 ? `${unresolved} boxes still need a decision` : undefined}><Send className="h-4 w-4" />Submit for approval</button>}
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

      <div className="scan-result-split grid gap-4 lg:grid-cols-[minmax(0,65fr)_minmax(320px,35fr)]" style={{ '--image-pane': `${panePct}fr` } as CSSProperties}>
        <div className="space-y-3">
          <label className="hidden items-center gap-2 text-[10px] font-semibold text-muted xl:flex">Image width
            <input type="range" min={45} max={75} value={panePct} onChange={(e) => setPanePct(Number(e.target.value))} aria-label="Resize image and results split" className="w-28 accent-brand" />
          </label>
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

        <div className="scan-results-panel rounded-2xl border border-line bg-white">
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
                      {selected.state === 'UNKNOWN' && !selected.assigned_sku ? (
                        <p className="rounded-xl border border-line px-3 py-2.5 text-xs text-muted">Unsure: no confident catalog match. Search the catalog below, or register it as a new product.</p>
                      ) : (
                      <ul className="space-y-1.5" aria-label="Suggested products">
                        {candidates.map((m, i) => (
                          <li key={m.sku}><button type="button" disabled={busy} onClick={() => act(i === 0 ? 'CONFIRM' : 'ASSIGN', m.sku)} className="flex min-h-11 w-full items-center justify-between rounded-xl border border-line-strong px-3 text-left text-sm hover:border-brand">
                            <span><kbd className="mr-2 rounded bg-cream px-1.5 font-mono text-[10px]">{i + 1}</kbd>{m.name ?? m.sku}</span><span className="font-mono text-xs text-muted">{Math.round(m.score * 100)}%</span></button></li>
                        ))}
                        {candidates.length === 0 && <li className="text-xs text-muted">No close matches in the catalog.</li>}
                      </ul>
                      )}
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
            <div className="space-y-3 p-3">
              <div className="rounded-xl border border-brand/20 bg-brand/5 p-3">
                <p className="text-xs font-bold text-ink">AI quantity estimates · human approval required</p>
                <p className="mt-1 text-xs leading-relaxed text-muted">
                  Quantities count visible detected packages. Unmatched packages stay under Unknown; add product reference photos for automatic SKU matching. Review flagged boxes before submitting for manager approval.
                </p>
                {canEdit && open.length > 0 && <button type="button" className={`${btnSecondary} mt-2 w-full`} onClick={() => { setTab('review'); setSel(open[0].id); }}>
                  Review flagged boxes ({open.length})
                </button>}
                {canEdit && open.length === 0 && <p className="mt-2 text-xs font-semibold text-brand">All flagged boxes are resolved. Submit for manager approval when ready.</p>}
                {p.can_approve && view.status === 'SUBMITTED' && <p className="mt-2 text-xs font-semibold text-brand">Review these estimates, then approve or return the scan.</p>}
              </div>
              <div className="space-y-2">
                {[...view.summary].sort((a, b) => a.variance - b.variance).map((r) => {
                  const flagged = openBySku.get(r.sku) ?? [];
                  return <article key={r.sku} className="min-w-0 rounded-xl border border-line p-3">
                    <div className="flex min-w-0 items-start justify-between gap-2">
                      <div className="min-w-0">
                        <p className="break-words text-xs font-semibold leading-snug">{r.name}</p>
                        <p className="mt-0.5 break-all font-mono text-[10px] text-muted">{r.sku}</p>
                      </div>
                      <span className={cn('shrink-0 rounded-full px-2 py-1 text-[9px] font-bold uppercase', r.status === 'SHORTFALL' ? 'bg-danger-soft text-danger' : r.status === 'SURPLUS' ? 'bg-warn-soft text-warn' : 'bg-brand-soft text-brand-deep')}>
                        {r.status === 'SHORTFALL' ? 'Short' : r.status === 'SURPLUS' ? 'Over' : 'OK'}
                      </span>
                    </div>
                    <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2">
                      {[['Est. qty', r.detected], ['Resolved', r.reviewed], ['Expected', r.expected], ['Variance', `${r.variance > 0 ? '+' : ''}${r.variance}`]].map(([label, value]) => (
                        <div key={label} className="flex min-w-0 items-baseline justify-between gap-1 border-t border-line pt-1.5">
                          <dt className="truncate text-[10px] text-muted">{label}</dt>
                          <dd className="shrink-0 font-mono text-xs font-semibold tabular-nums">{value}</dd>
                        </div>
                      ))}
                    </dl>
                    {flagged.length > 0 && canEdit && <button type="button" className="mt-2 max-w-full break-words text-left text-[10px] font-bold text-brand underline" onClick={() => { setTab('review'); setSel(flagged[0]); }}>Review {flagged.length} flagged {flagged.length === 1 ? 'box' : 'boxes'}</button>}
                  </article>;
                })}
                {unmatched.length > 0 && <article className="min-w-0 rounded-xl border border-warn/30 bg-warn-soft/30 p-3">
                  <div className="flex min-w-0 items-start justify-between gap-2">
                    <p className="min-w-0 break-words text-xs font-semibold">Unknown / no catalog match</p>
                    <span className="shrink-0 rounded-full bg-warn-soft px-2 py-1 text-[9px] font-bold uppercase text-warn">Needs review</span>
                  </div>
                  <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2">
                    <div className="flex min-w-0 items-baseline justify-between gap-1 border-t border-line pt-1.5"><dt className="truncate text-[10px] text-muted">Est. qty</dt><dd className="font-mono text-xs font-semibold tabular-nums">{unmatched.length}</dd></div>
                    <div className="flex min-w-0 items-baseline justify-between gap-1 border-t border-line pt-1.5"><dt className="truncate text-[10px] text-muted">Resolved</dt><dd className="font-mono text-xs font-semibold tabular-nums">{unmatched.filter((d) => d.accepted_unknown).length}</dd></div>
                  </dl>
                  {canEdit && <button type="button" className="mt-2 max-w-full break-words text-left text-[10px] font-bold text-brand underline" onClick={() => { setTab('review'); setSel(unmatched.find(isOpen)?.id ?? unmatched[0].id); }}>Review unknown items</button>}
                </article>}
                {view.summary.length === 0 && unmatched.length === 0 && <p className="p-6 text-center text-xs text-muted">No item estimates are available yet.</p>}
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
            {sheet === 'submit' && <ul className="mt-2 space-y-1 text-sm text-muted"><li>✓ All flagged boxes resolved</li><li>{shortfalls.length ? `⚠ ${shortfalls.length} SKU${shortfalls.length > 1 ? 's' : ''} short of expected` : '✓ No shortfalls beyond tolerance'}</li></ul>}
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
