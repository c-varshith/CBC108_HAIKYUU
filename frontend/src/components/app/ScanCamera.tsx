import { Camera, CameraOff, Loader2, Pause, Play, RefreshCw, SwitchCamera, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { ApiError } from '../../lib/http';
import { useCamera } from '../../lib/camera';
import { scans } from '../../lib/api';
import type { LiveFrameResult } from '../../lib/contract';
import { btnPrimary, btnSecondary } from '../ui';
import { Notice, errMsg } from './shared';

const FRAME_INTERVAL_MS = 2000;
const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

/** Authenticated live preview. Frames are transient; freezing creates a normal scan for human review. */
export function ScanCamera({ onFreeze, onClose }: { onFreeze: (file: File) => void; onClose: () => void }) {
  const { videoRef, state, facing, start, stop, flip, capture } = useCamera();
  const [available, setAvailable] = useState<boolean | null>(null);
  const [unavailableReason, setUnavailableReason] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<LiveFrameResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [frames, setFrames] = useState(0);
  const active = state.status === 'active';

  useEffect(() => {
    let alive = true;
    void scans.liveStatus().then((status) => {
      if (!alive) return;
      setAvailable(status.available);
      setUnavailableReason(status.reason);
    }).catch((e) => {
      if (!alive) return;
      setAvailable(false);
      setUnavailableReason(`Could not check live detection availability: ${errMsg(e)}`);
    });
    void start();
    return () => { alive = false; stop(); };
    // Camera starts only after the user opens this panel; start is refreshed when the camera facing changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!running || !active || available !== true) return;
    let cancelled = false;
    const controller = new AbortController();
    setError(null);
    (async () => {
      let consecutiveErrors = 0;
      while (!cancelled) {
        if (document.hidden) {
          await sleep(500);
          continue;
        }
        const startedAt = performance.now();
        const frame = await capture(1280, 0.8);
        if (cancelled) return;
        if (!frame) {
          await sleep(250);
          continue;
        }
        try {
          const next = await scans.liveDetect(frame.blob, controller.signal);
          if (cancelled) return;
          consecutiveErrors = 0;
          setResult(next);
          setFrames((n) => n + 1);
          setError(null);
        } catch (e) {
          if (cancelled || (e instanceof DOMException && e.name === 'AbortError')) return;
          if (e instanceof ApiError && e.status === 503) {
            setAvailable(false);
            setUnavailableReason(e.message);
            setRunning(false);
            return;
          }
          consecutiveErrors += 1;
          if (consecutiveErrors >= 3) {
            const message = errMsg(e);
            setError(message);
            setAvailable(false);
            setUnavailableReason(`Live detection stopped because the AI worker could not analyze camera frames. You can still capture a photo and run a normal scan. ${message}`);
            setRunning(false);
            return;
          }
        }
        await sleep(Math.max(0, FRAME_INTERVAL_MS - (performance.now() - startedAt)));
      }
    })();
    return () => { cancelled = true; controller.abort(); };
  }, [active, available, capture, running]);

  async function freeze() {
    const frame = await capture(4000, 0.92);
    if (!frame) { setError('The camera frame is not ready yet. Try again in a moment.'); return; }
    const file = new File([frame.blob], `camera-${new Date().toISOString().replace(/[:.]/g, '-')}.jpg`, { type: 'image/jpeg' });
    setRunning(false);
    stop();
    onFreeze(file);
    onClose();
  }

  const total = result?.estimates.reduce((sum, estimate) => sum + estimate.quantity, 0) ?? 0;

  return (
    <section className="space-y-3" aria-label="Live shelf camera">
      <div className="relative overflow-hidden rounded-xl bg-ink" style={{ aspectRatio: active && state.width && state.height ? `${state.width} / ${state.height}` : '16 / 9' }}>
        <video ref={videoRef} playsInline muted aria-label="Live shelf camera preview" className="absolute inset-0 h-full w-full object-fill" />
        {active && result && result.detections.map((d) => {
          const [x1, y1, x2, y2] = d.bbox;
          const uncertain = d.state !== 'SURE';
          const name = d.assigned_sku ?? d.top_matches[0]?.name ?? d.top_matches[0]?.sku ?? 'Unknown';
          return <div key={d.id} className="pointer-events-none absolute" style={{ left: `${x1 / result.width * 100}%`, top: `${y1 / result.height * 100}%`, width: `${(x2 - x1) / result.width * 100}%`, height: `${(y2 - y1) / result.height * 100}%` }}>
            <div className="absolute inset-0 border-2" style={{ borderColor: uncertain ? '#fbbf24' : '#34d399', background: uncertain ? 'rgba(251,191,36,.08)' : 'rgba(52,211,153,.08)' }} />
            <span className="absolute left-0 top-0 max-w-full -translate-y-full truncate rounded bg-ink/90 px-1 py-0.5 text-[9px] font-semibold text-white">{name}</span>
          </div>;
        })}
        {state.status === 'requesting' && <div className="absolute inset-0 grid place-items-center bg-ink/75 text-sm font-semibold text-white"><Loader2 className="mr-2 inline h-4 w-4 animate-spin" />Waiting for camera permission…</div>}
        {state.status === 'error' && <div role="alert" className="absolute inset-0 grid place-items-center bg-ink/90 p-5 text-center text-sm text-white">
          <div><CameraOff className="mx-auto h-7 w-7 text-amber-300" /><p className="mt-2">{state.message}</p>{state.kind !== 'insecure' && state.kind !== 'unsupported' && <button type="button" className={`${btnSecondary} mt-3`} onClick={() => void start()}><RefreshCw className="h-4 w-4" />Try again</button>}</div>
        </div>}
        {running && <div className="absolute left-2 top-2 rounded-full bg-ink/80 px-2.5 py-1 font-mono text-[10px] font-bold uppercase text-emerald-200"><span className="mr-1.5 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />Live · {frames} frames</div>}
      </div>

      <div className="flex flex-wrap gap-2">
        <button type="button" disabled={!active || available !== true} onClick={() => { setError(null); setRunning((value) => !value); }} className={btnPrimary} title={available === false ? unavailableReason ?? undefined : undefined}>
          {running ? <><Pause className="h-4 w-4" />Pause detection</> : <><Play className="h-4 w-4" />Start live detection</>}
        </button>
        <button type="button" disabled={!active} onClick={() => void freeze()} className={btnSecondary}><Camera className="h-4 w-4" />Freeze &amp; review</button>
        <button type="button" disabled={state.status === 'requesting'} onClick={() => void flip()} aria-label={`Switch camera (now ${facing === 'environment' ? 'rear' : 'front'})`} className={btnSecondary}><SwitchCamera className="h-4 w-4" /></button>
        <button type="button" onClick={() => { setRunning(false); stop(); onClose(); }} className="ml-auto inline-flex items-center gap-2 rounded-full px-3 py-2 text-sm font-semibold text-muted hover:text-ink"><X className="h-4 w-4" />Close</button>
      </div>

      {available === null && <p className="text-xs text-muted" role="status">Checking AI worker…</p>}
      {available === false && unavailableReason && <Notice tone="warn">{unavailableReason}</Notice>}
      {error && <Notice>{error}</Notice>}
      {result && <div className="rounded-xl border border-line" aria-live="polite">
        <div className="flex items-center justify-between border-b border-line px-3 py-2 text-xs font-bold"><span>Live quantity estimate</span><span className="font-mono text-brand">{total} items</span></div>
        {result.estimates.length ? <ul className="divide-y divide-line">{result.estimates.map((item) => <li key={item.sku ?? 'unknown'} className="flex min-w-0 items-center justify-between gap-2 px-3 py-2 text-xs">
          <span className="min-w-0 break-words font-semibold">{item.name}{item.needs_review > 0 && <span className="ml-1 font-normal text-warn">· {item.needs_review} check</span>}</span>
          <span className="shrink-0 font-mono tabular-nums">×{item.quantity}</span>
        </li>)}</ul> : <p className="px-3 py-3 text-xs text-muted">No items detected in this frame.</p>}
      </div>}
    </section>
  );
}
