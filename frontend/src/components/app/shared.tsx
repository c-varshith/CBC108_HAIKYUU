import { AlertTriangle, CheckCircle2, CircleHelp, Pencil } from 'lucide-react';
import { memo, useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { DetState, Detection, ScanStatus } from '../../lib/contract';
import { cn } from '../../utils/cn';

export const go = (path: string) => { window.location.hash = `#/app${path ? `/${path}` : ''}`; };

export function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : 'Something went wrong.';
}

export function Notice({ tone = 'danger', children }: { tone?: 'danger' | 'warn' | 'brand'; children: ReactNode }) {
  const t = { danger: 'border-danger/25 bg-danger-soft text-danger', warn: 'border-warn/25 bg-warn-soft text-warn', brand: 'border-brand/25 bg-brand-mist text-brand-deep' }[tone];
  return <div role="alert" className={cn('rounded-xl border px-4 py-3 text-sm font-medium', t)}>{children}</div>;
}

export function Spinner({ label = 'Loading…' }: { label?: string }) {
  return <p className="py-10 text-center text-sm text-muted" role="status">{label}</p>;
}

/* ---- state styling: colour + icon + outline style, never colour alone (spec 1.6) ---- */
export const STATE_STYLE: Record<DetState, { color: string; line: 'solid' | 'dashed' | 'dotted' | 'double'; label: string; Icon: typeof CheckCircle2 }> = {
  SURE: { color: '#22C55E', line: 'solid', label: 'Sure', Icon: CheckCircle2 },
  CHECK: { color: '#F5B301', line: 'dashed', label: 'Check', Icon: AlertTriangle },
  UNKNOWN: { color: '#FF4D4F', line: 'dotted', label: 'Unsure · needs review', Icon: CircleHelp },
  CORRECTED: { color: '#0EA5C6', line: 'double', label: 'Corrected', Icon: Pencil },
};

export function StateChip({ state, count }: { state: DetState; count?: number }) {
  const s = STATE_STYLE[state];
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-bold" style={{ borderColor: s.color, color: s.color }}>
      <s.Icon className="h-3.5 w-3.5" aria-hidden />
      {s.label}{count !== undefined && <span className="font-mono tabular-nums">{count}</span>}
    </span>
  );
}

const STATUS_TONE: Partial<Record<ScanStatus, string>> = {
  APPROVED: 'bg-brand-soft text-brand-deep', SUBMITTED: 'bg-sky-100 text-sky-800', RETURNED: 'bg-warn-soft text-warn',
  FAILED: 'bg-danger-soft text-danger', NEEDS_REVIEW: 'bg-amber-100 text-amber-800', IN_REVIEW: 'bg-amber-100 text-amber-800',
};
export const STATUS_HINT: Record<ScanStatus, string> = {
  DRAFT: 'Not uploaded yet.', UPLOADED: 'Waiting to be processed.', PROCESSING: 'Detecting and matching items…',
  NEEDS_REVIEW: 'Review the flagged boxes, then submit.', IN_REVIEW: 'Keep reviewing, then submit for approval.',
  AUTO_READY: 'Nothing to review. Submit for approval.', SUBMITTED: 'Waiting for a manager to approve or return it.',
  APPROVED: 'Approved and locked. It feeds reports and alerts.', RETURNED: 'Returned with a note. Fix the pinned boxes and resubmit.',
  REOPENED: 'Reopened for changes.', FAILED: 'Processing failed. Retry it.', ARCHIVED: 'Archived.',
};
export function StatusBadge({ status }: { status: ScanStatus }) {
  return <span className={cn('inline-flex rounded-full px-2.5 py-1 font-mono text-[10px] font-bold uppercase tracking-wide', STATUS_TONE[status] ?? 'bg-cream text-muted')}>{status.replace('_', ' ')}</span>;
}

/** Only http(s) links from the server are rendered as links (a stored `javascript:` URL must never become an href). */
export const safeHref = (u?: string | null) => (u && /^https?:\/\//i.test(u) ? u : undefined);

export const fmtDate = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });

/** Image with HUD-style boxes. bbox is in ORIGINAL image pixels; percentages keep it correct at any size/orientation. */
export const ScanCanvas = memo(function ScanCanvas(props: {
  src: string; width: number; height: number; detections: Detection[]; selectedId: string | null; pinned?: string[];
  filter: DetState | 'ALL'; showLabels: boolean; onSelect: (id: string) => void; scanning?: boolean;
  selectedIds?: string[]; onMultiSelect?: (ids: string[]) => void;
}) {
  const { src, width, height, detections, selectedId, pinned = [], filter, showLabels, onSelect, scanning,
    selectedIds = [], onMultiSelect } = props;
  const fitHost = useRef<HTMLDivElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const drag = useRef<{ mode: 'select' | 'pan'; x: number; y: number; clientX: number; clientY: number } | null>(null);
  const touches = useRef(new Map<number, [number, number]>());
  const pinch = useRef<{ distance: number; zoom: number } | null>(null);
  const space = useRef(false);
  const [layout, setLayout] = useState({ w: 0, h: 0 });
  const [frameWidth, setFrameWidth] = useState<number | null>(null);
  const [marquee, setMarquee] = useState<[number, number, number, number] | null>(null);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [menu, setMenu] = useState<{ x: number; y: number; id: string } | null>(null);

  useEffect(() => {
    const host = fitHost.current;
    if (!host) return;
    const fit = () => {
      const maxHeight = window.innerWidth < 768 ? window.innerHeight * 0.42 : window.innerHeight * 0.5;
      setFrameWidth(Math.min(host.clientWidth, maxHeight * width / Math.max(height, 1)));
    };
    const ro = new ResizeObserver(fit);
    ro.observe(host);
    window.addEventListener('resize', fit);
    fit();
    return () => { ro.disconnect(); window.removeEventListener('resize', fit); };
  }, [height, width]);

  useEffect(() => {
    const el = stage.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setLayout({ w: entry.contentRect.width, h: entry.contentRect.height }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const c = canvas.current;
    if (!c || !layout.w || !layout.h) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = Math.round(layout.w * dpr);
    c.height = Math.round(layout.h * dpr);
    const ctx = c.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(c.width / width, 0, 0, c.height / height, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const shown = detections.filter((d) => !d.deleted && (filter === 'ALL' || d.state === filter));
    for (const d of shown) {
      const [x1, y1, x2, y2] = d.bbox;
      const state = STATE_STYLE[d.state];
      const active = d.id === selectedId || selectedIds.includes(d.id);
      const color = state.color;
      const sw = Math.max(2, width / layout.w * 1.5);
      ctx.save();
      ctx.strokeStyle = active ? '#ffffff' : color;
      ctx.lineWidth = sw;
      ctx.setLineDash(state.line === 'dashed' ? [sw * 3, sw * 2] : state.line === 'dotted' ? [sw, sw * 2] : []);
      if (active) { ctx.shadowColor = color; ctx.shadowBlur = sw * 4; }
      const corner = Math.min((x2 - x1) * 0.25, (y2 - y1) * 0.25, width / 80);
      ctx.beginPath();
      ctx.moveTo(x1, y1 + corner); ctx.lineTo(x1, y1); ctx.lineTo(x1 + corner, y1);
      ctx.moveTo(x2 - corner, y1); ctx.lineTo(x2, y1); ctx.lineTo(x2, y1 + corner);
      ctx.moveTo(x1, y2 - corner); ctx.lineTo(x1, y2); ctx.lineTo(x1 + corner, y2);
      ctx.moveTo(x2 - corner, y2); ctx.lineTo(x2, y2); ctx.lineTo(x2, y2 - corner);
      ctx.stroke();
      if (pinned.includes(d.id)) {
        ctx.shadowBlur = 0; ctx.strokeStyle = '#e8512f'; ctx.lineWidth = sw;
        ctx.setLineDash([]); ctx.strokeRect(x1 + sw * 2, y1 + sw * 2, x2 - x1 - sw * 4, y2 - y1 - sw * 4);
      }
      const productName = d.assigned_name ?? d.top_matches[0]?.name ?? d.assigned_sku ?? d.top_matches[0]?.sku ?? 'Unknown';
      if (active || (showLabels && shown.length < 80)) {
        const font = Math.max(10, width / layout.w * 10);
        ctx.font = `bold ${font}px monospace`;
        const label = `${d.state} · ${productName.length > 30 ? `${productName.slice(0, 29)}…` : productName}`;
        const labelY = Math.max(font + 5, y1);
        ctx.fillStyle = 'rgba(5,14,36,.92)';
        ctx.fillRect(x1, labelY - font - 6, ctx.measureText(label).width + 10, font + 8);
        ctx.fillStyle = '#fff'; ctx.fillText(label, x1 + 5, labelY - 2);
      }
      ctx.restore();
    }
    if (marquee) {
      const [x1, y1, x2, y2] = marquee;
      ctx.save(); ctx.strokeStyle = '#0891b2'; ctx.fillStyle = 'rgba(8,145,178,.14)';
      ctx.lineWidth = Math.max(2, width / layout.w); ctx.setLineDash([ctx.lineWidth * 4, ctx.lineWidth * 2]);
      ctx.fillRect(x1, y1, x2 - x1, y2 - y1); ctx.strokeRect(x1, y1, x2 - x1, y2 - y1); ctx.restore();
    }
  }, [detections, filter, height, layout, marquee, pinned, selectedId, selectedIds, showLabels, width]);

  useEffect(() => {
    const down = (e: KeyboardEvent) => { if (e.code === 'Space' && !e.repeat) space.current = true; };
    const up = (e: KeyboardEvent) => { if (e.code === 'Space') space.current = false; };
    window.addEventListener('keydown', down); window.addEventListener('keyup', up);
    return () => { window.removeEventListener('keydown', down); window.removeEventListener('keyup', up); };
  }, []);

  const point = useCallback((e: React.PointerEvent) => {
    const rect = stage.current?.getBoundingClientRect();
    if (!rect) return [0, 0] as const;
    return [((e.clientX - rect.left) / rect.width) * width, ((e.clientY - rect.top) / rect.height) * height] as const;
  }, [height, width]);
  const hit = useCallback((x: number, y: number) => [...detections].reverse().find((d) => !d.deleted &&
    (filter === 'ALL' || d.state === filter) && x >= d.bbox[0] && x <= d.bbox[2] && y >= d.bbox[1] && y <= d.bbox[3]), [detections, filter]);
  function pointerDown(e: React.PointerEvent<HTMLCanvasElement>) {
    setMenu(null);
    if (e.pointerType === 'touch') {
      touches.current.set(e.pointerId, [e.clientX, e.clientY]);
      if (touches.current.size === 2) {
        const [[, a], [, b]] = [...touches.current.entries()];
        pinch.current = { distance: Math.hypot(a[0] - b[0], a[1] - b[1]), zoom };
        drag.current = null; setMarquee(null); e.preventDefault(); return;
      }
    }
    const [x, y] = point(e);
    if (space.current || e.button === 1) {
      drag.current = { mode: 'pan', x, y, clientX: e.clientX, clientY: e.clientY };
      e.currentTarget.setPointerCapture(e.pointerId); e.preventDefault(); return;
    }
    const box = hit(x, y);
    if (box) {
      onSelect(box.id);
      if (e.shiftKey && onMultiSelect) onMultiSelect(selectedIds.includes(box.id) ? selectedIds.filter((id) => id !== box.id) : [...selectedIds, box.id]);
      else onMultiSelect?.([box.id]);
      return;
    }
    drag.current = { mode: 'select', x, y, clientX: e.clientX, clientY: e.clientY };
    setMarquee([x, y, x, y]); e.currentTarget.setPointerCapture(e.pointerId);
  }
  function pointerMove(e: React.PointerEvent<HTMLCanvasElement>) {
    if (e.pointerType === 'touch' && touches.current.has(e.pointerId)) {
      touches.current.set(e.pointerId, [e.clientX, e.clientY]);
      if (pinch.current && touches.current.size >= 2) {
        const [[, a], [, b]] = [...touches.current.entries()];
        const distance = Math.hypot(a[0] - b[0], a[1] - b[1]);
        setZoom(Math.max(1, Math.min(4, pinch.current.zoom * distance / Math.max(pinch.current.distance, 1))));
        e.preventDefault(); return;
      }
    }
    const d = drag.current;
    if (!d) return;
    if (d.mode === 'pan') setPan((p) => ({ x: p.x + e.clientX - d.clientX, y: p.y + e.clientY - d.clientY }));
    else { const [x, y] = point(e); setMarquee([Math.min(d.x, x), Math.min(d.y, y), Math.max(d.x, x), Math.max(d.y, y)]); }
    d.clientX = e.clientX; d.clientY = e.clientY;
  }
  function pointerUp(e: React.PointerEvent<HTMLCanvasElement>) {
    if (e.pointerType === 'touch') {
      touches.current.delete(e.pointerId);
      if (pinch.current) { pinch.current = null; drag.current = null; setMarquee(null); return; }
    }
    const d = drag.current; drag.current = null;
    if (!d || d.mode !== 'select') return;
    const [x, y] = point(e);
    const box = [Math.min(d.x, x), Math.min(d.y, y), Math.max(d.x, x), Math.max(d.y, y)];
    setMarquee(null);
    if (Math.abs(box[2] - box[0]) < 4 && Math.abs(box[3] - box[1]) < 4) { onMultiSelect?.([]); return; }
    const ids = detections.filter((det) => !det.deleted && (filter === 'ALL' || det.state === filter) && det.bbox[0] <= box[2] && det.bbox[2] >= box[0] && det.bbox[1] <= box[3] && det.bbox[3] >= box[1]).map((det) => det.id);
    onMultiSelect?.(ids);
    if (ids[0]) onSelect(ids[0]);
  }
  function contextMenu(e: React.MouseEvent<HTMLCanvasElement>) {
    e.preventDefault();
    const rect = stage.current?.getBoundingClientRect();
    if (!rect) return;
    const x = (e.clientX - rect.left) / rect.width * width, y = (e.clientY - rect.top) / rect.height * height;
    const box = hit(x, y);
    if (box) { onSelect(box.id); onMultiSelect?.([box.id]); setMenu({ x: e.clientX, y: e.clientY, id: box.id }); }
  }
  function zoomAt(direction: 'in' | 'out') { setZoom((z) => Math.max(1, Math.min(4, z * (direction === 'in' ? 1.12 : 0.89)))); }
  function moveMinimap(e: React.MouseEvent<HTMLButtonElement>) {
    if (zoom <= 1) return;
    const r = e.currentTarget.getBoundingClientRect();
    const px = (e.clientX - r.left) / r.width, py = (e.clientY - r.top) / r.height;
    setPan({ x: (0.5 - px) * r.width * (zoom - 1), y: (0.5 - py) * r.height * (zoom - 1) });
  }
  const similar = menu ? detections.filter((d) => !d.deleted && d.top_matches[0]?.sku === detections.find((x) => x.id === menu.id)?.top_matches[0]?.sku).map((d) => d.id) : [];
  return (
    <div ref={fitHost} className="w-full">
    <div className="relative mx-auto select-none overflow-hidden rounded-xl bg-[#0a1226]" style={{ width: frameWidth ?? '100%', aspectRatio: `${width} / ${height}`, touchAction: 'none' }}>
      <div ref={stage} className="absolute inset-0 origin-center" style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})` }}>
        <img src={src} alt="Shelf photo with detected items" draggable={false} className="block h-full w-full object-fill" />
        <canvas ref={canvas} tabIndex={0} aria-label={`Shelf image with ${detections.filter((d) => !d.deleted).length} detections. Use wheel to zoom and space plus drag to pan.`}
          onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={pointerUp} onPointerCancel={pointerUp} onContextMenu={contextMenu}
          onWheel={(e) => { e.preventDefault(); zoomAt(e.deltaY < 0 ? 'in' : 'out'); }} className="absolute inset-0 h-full w-full cursor-crosshair outline-none" />
      </div>
      {zoom > 1 && <button type="button" onClick={moveMinimap} aria-label="Minimap: click to navigate the image" title="Minimap · click to navigate"
        className="absolute bottom-2 right-2 h-20 w-28 overflow-hidden rounded border border-white/70 bg-slate-900/70 shadow-lg" style={{ backgroundImage: `url(${src})`, backgroundSize: '100% 100%' }}>
        <span className="absolute inset-0 border border-cyan-200/40" />
        <span className="absolute border-2 border-cyan-300 bg-cyan-300/10" style={{ width: `${100 / zoom}%`, height: `${100 / zoom}%`, left: `${Math.max(0, Math.min(100 - 100 / zoom, 50 - pan.x / Math.max(layout.w, 1) * 100 - 50 / zoom))}%`, top: `${Math.max(0, Math.min(100 - 100 / zoom, 50 - pan.y / Math.max(layout.h, 1) * 100 - 50 / zoom))}%` }} />
      </button>}
      <div className="absolute right-2 top-2 flex gap-1 rounded-lg bg-slate-950/75 p-1 text-white">
        <button type="button" aria-label="Zoom in" className="h-7 w-7 rounded hover:bg-white/20" onClick={() => zoomAt('in')}>+</button>
        <span className="grid min-w-10 place-items-center font-mono text-[10px]">{Math.round(zoom * 100)}%</span>
        <button type="button" aria-label="Zoom out" className="h-7 w-7 rounded hover:bg-white/20" onClick={() => zoomAt('out')}>−</button>
        <button type="button" aria-label="Reset zoom" className="h-7 rounded px-1 text-[10px] hover:bg-white/20" onClick={() => { setZoom(1); setPan({ x: 0, y: 0 }); }}>Fit</button>
      </div>
      {menu && <div role="menu" className="fixed z-50 min-w-44 rounded-xl border border-line bg-white p-1.5 text-xs shadow-pop" style={{ left: menu.x, top: menu.y }}>
        <button type="button" role="menuitem" className="block w-full rounded-lg px-3 py-2 text-left hover:bg-cream" onClick={() => { onMultiSelect?.(similar); setMenu(null); }}>Select {similar.length} with same suggestion</button>
        <button type="button" role="menuitem" className="block w-full rounded-lg px-3 py-2 text-left text-muted hover:bg-cream" onClick={() => setMenu(null)}>Close menu</button>
      </div>}
      {scanning && <div aria-hidden className="pointer-events-none absolute inset-x-0 top-0 h-1/3 scan-sweep"><div className="h-full w-full bg-gradient-to-b from-transparent via-cyan-300/20 to-cyan-200/45" /><div className="absolute inset-x-0 bottom-0 h-0.5 bg-cyan-100 shadow-[0_0_18px_3px_rgba(34,211,238,0.8)]" /></div>}
    </div>
    </div>
  );
});

/** Cropped preview of one detection from the (display-sized) image using CSS background mapping. */
export function Crop({ src, width, height, bbox, className }: { src: string; width: number; height: number; bbox: Detection['bbox']; className?: string }) {
  const [x1, y1, x2, y2] = bbox;
  const bw = Math.max((x2 - x1) / width, 0.01), bh = Math.max((y2 - y1) / height, 0.01);
  const px = bw >= 0.999 ? 0 : (x1 / width / (1 - bw)) * 100, py = bh >= 0.999 ? 0 : (y1 / height / (1 - bh)) * 100;
  return <span aria-hidden className={cn('block rounded-lg border border-line bg-ink', className)}
    style={{ backgroundImage: `url(${src})`, backgroundSize: `${100 / bw}% ${100 / bh}%`, backgroundPosition: `${px}% ${py}%`, backgroundRepeat: 'no-repeat' }} />;
}
