import { Camera, ImagePlus, UploadCloud, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { catalog, scans } from '../../lib/api';
import type { Shelf } from '../../lib/contract';
import { btnPrimary, btnSecondary } from '../ui';
import { ScanCamera } from './ScanCamera';
import { Notice, Spinner, errMsg, go } from './shared';

const MAX_MB = 25;
const OK_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif'];

/** Capture/upload. One scan is created per photo; the shelf is required because expected stock is per shelf. */
export function ScanNew() {
  const [shelves, setShelves] = useState<Shelf[] | null>(null);
  const [shelf, setShelf] = useState(() => localStorage.getItem('cx.shelf') ?? '');
  const [files, setFiles] = useState<File[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [offlineMessage, setOfflineMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const [cameraOpen, setCameraOpen] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const cam = useRef<HTMLInputElement>(null);
  const requestIds = useRef(new WeakMap<File, string>());

  useEffect(() => {
    catalog.shelves().then((s) => { setShelves(s); setShelf((cur) => (s.some((x) => x.id === cur) ? cur : s[0]?.id ?? '')); }).catch((e) => setError(errMsg(e)));
  }, []);

  const add = useCallback((list: FileList | File[]) => {
    const next: File[] = [];
    for (const f of Array.from(list)) {
      const heic = /\.(heic|heif)$/i.test(f.name);
      if (!OK_TYPES.includes(f.type) && !heic) { setError(`${f.name} is not a supported image (use JPEG, PNG, WebP or HEIC).`); continue; }
      if (f.size > MAX_MB * 1024 * 1024) { setError(`${f.name} is larger than ${MAX_MB} MB.`); continue; }
      next.push(f);
    }
    if (next.length) { setError(null); setFiles((cur) => [...cur, ...next].slice(0, 10)); }
  }, []);

  // Ctrl+V paste of a screenshot or copied image (desktop)
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => { if (e.clipboardData?.files.length) add(e.clipboardData.files); };
    window.addEventListener('paste', onPaste);
    return () => window.removeEventListener('paste', onPaste);
  }, [add]);

  async function start() {
    if (!shelf || files.length === 0) return;
    let queuedIds: string[] = [];
    setBusy(true);
    setError(null);
    setOfflineMessage(null);
    try {
      localStorage.setItem('cx.shelf', shelf);
      const ids = files.map((file) => {
        const existing = requestIds.current.get(file);
        if (existing) return existing;
        const id = crypto.randomUUID(); requestIds.current.set(file, id); return id;
      });
      if (!navigator.onLine) {
        const { queueUploads } = await import('../../lib/uploadQueue');
        const count = await queueUploads(shelf, files);
        setFiles([]);
        setOfflineMessage(`${files.length} photo${files.length === 1 ? '' : 's'} saved on this device. ${count} upload${count === 1 ? '' : 's'} will send when you’re back online.`);
        window.dispatchEvent(new Event('cx:queue-change'));
        setBusy(false);
        return;
      }
      // Persist the request ids and photo bytes before sending. If the server commits
      // but the response is lost, the queue can safely replay the same id after reload.
      const queue = await import('../../lib/uploadQueue');
      const pending = await queue.queueFiles(shelf, files, ids);
      queuedIds = pending.map((item) => item.id);
      window.dispatchEvent(new Event('cx:queue-change'));
      const r = await scans.create(shelf, files, navigator.userAgent.includes('Mobi') ? 'Phone' : 'Desktop', ids);
      await Promise.all(pending.map((item) => queue.removeQueuedUpload(item.id)));
      window.dispatchEvent(new Event('cx:queue-change'));
      // Several photos make several scans: open the first, the rest wait in the scan list.
      go(r.scan_ids.length > 1 ? 'scans' : `scans/${r.scan_id}`);
    } catch (e) {
      setError(errMsg(e));
      const status = (e as { status?: number }).status;
      if ([400, 413, 422].includes(status ?? 0) && queuedIds.length) {
        const { removeQueuedUpload } = await import('../../lib/uploadQueue');
        await Promise.all(queuedIds.map(removeQueuedUpload));
        window.dispatchEvent(new Event('cx:queue-change'));
      } else if (queuedIds.length) {
        setOfflineMessage('Upload confirmation was interrupted. The photo is saved on this device and will retry safely.');
      }
      setBusy(false);
    }
  }

  if (!shelves && !error) return <Spinner />;
  return (
    <div className="mx-auto max-w-2xl space-y-5">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Scan a shelf</h1>
        <p className="mt-1 text-sm text-muted">Take a wide photo of the whole shelf. Doubtful items get flagged for you to check.</p>
      </div>
      {shelves && shelves.length === 0 && <Notice tone="warn">No shelves exist yet. An Owner needs to add a store and shelf first.</Notice>}
      <label className="block text-xs font-semibold">Shelf / location
        <select value={shelf} onChange={(e) => setShelf(e.target.value)} className="mt-1 w-full rounded-xl border border-line-strong bg-white px-3.5 py-3 text-sm">
          {(shelves ?? []).map((s) => <option key={s.id} value={s.id}>{s.store_name ? `${s.store_name} · ` : ''}{s.name}</option>)}
        </select>
      </label>

      {cameraOpen ? <ScanCamera onFreeze={(file) => { add([file]); setCameraOpen(false); }} onClose={() => setCameraOpen(false)} /> : <div
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); add(e.dataTransfer.files); }}
        className={`rounded-2xl border-2 border-dashed p-8 text-center transition-colors ${drag ? 'border-brand bg-brand-mist' : 'border-line-strong bg-white'}`}
      >
        <UploadCloud className="mx-auto h-9 w-9 text-brand" aria-hidden />
        <p className="mt-3 text-sm font-semibold">Drop photos here, paste with Ctrl+V, or choose</p>
        <p className="mt-1 text-xs text-muted">JPEG, PNG, WebP or HEIC · up to {MAX_MB} MB · up to 10 photos</p>
        <div className="mt-4 flex flex-wrap justify-center gap-2">
          <button type="button" className={btnPrimary} onClick={() => cam.current?.click()}><Camera className="h-4 w-4" />Take photo</button>
          <button type="button" className={btnSecondary} disabled={!navigator.onLine} onClick={() => setCameraOpen(true)} title={!navigator.onLine ? 'Connect to the network to use live detection.' : undefined}><Camera className="h-4 w-4" />Live camera</button>
          <button type="button" className={btnSecondary} onClick={() => input.current?.click()}><ImagePlus className="h-4 w-4" />Choose files</button>
        </div>
        <input ref={cam} type="file" accept="image/*" capture="environment" hidden onChange={(e) => { if (e.target.files) add(e.target.files); e.target.value = ''; }} />
        <input ref={input} type="file" accept="image/*,.heic,.heif" multiple hidden onChange={(e) => { if (e.target.files) add(e.target.files); e.target.value = ''; }} />
      </div>}

      {files.length > 0 && (
        <ul className="grid grid-cols-3 gap-2 sm:grid-cols-4">
          {files.map((f, i) => (
            <li key={`${f.name}-${i}`} className="relative overflow-hidden rounded-xl border border-line bg-white">
              {f.type.startsWith('image/') && f.type !== 'image/heic' && f.type !== 'image/heif'
                ? <img src={URL.createObjectURL(f)} alt={f.name} className="aspect-square w-full object-cover" onLoad={(e) => URL.revokeObjectURL((e.target as HTMLImageElement).src)} />
                : <div className="grid aspect-square place-items-center text-xs text-muted">{f.name}</div>}
              <button type="button" aria-label={`Remove ${f.name}`} onClick={() => setFiles((c) => c.filter((_, j) => j !== i))} className="absolute right-1 top-1 grid h-7 w-7 place-items-center rounded-full bg-ink/80 text-white"><X className="h-3.5 w-3.5" /></button>
            </li>
          ))}
        </ul>
      )}
      {error && <Notice>{error}</Notice>}
      {offlineMessage && <Notice tone="warn">{offlineMessage}</Notice>}
      {!navigator.onLine && <Notice tone="warn">You’re offline. Uploads will be saved on this device until the connection returns.</Notice>}
      <button type="button" disabled={busy || !shelf || files.length === 0} onClick={start} className={`${btnPrimary} w-full !py-3.5`}>
        {busy ? 'Saving…' : !navigator.onLine ? `Queue ${files.length || ''} photo${files.length === 1 ? '' : 's'} for upload` : `Upload ${files.length || ''} photo${files.length === 1 ? '' : 's'} and count`}
      </button>
    </div>
  );
}
