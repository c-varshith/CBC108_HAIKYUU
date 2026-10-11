import { useCallback, useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { catalog, settings, users } from '../../lib/api';
import { ROLE_LABEL } from '../../lib/auth';
import type { AppUser, Role, Shelf, Store } from '../../lib/contract';
import { btnPrimary, btnSecondary } from '../ui';
import { Notice, Spinner, errMsg } from './shared';

const ROLES: Role[] = ['OWNER', 'MANAGER', 'STAFF', 'AUDITOR'];
const field = 'rounded-xl border border-line-strong bg-white px-3 py-2.5 text-sm';
type Cfg = { thresholds: { sure: number; margin: number; check: number }; tolerance: { pct: number; units: number }; auto_approve: boolean; retention_days: number };

function Thresholds() {
  const [cfg, setCfg] = useState<Cfg | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [preview, setPreview] = useState<Awaited<ReturnType<typeof settings.previewThresholds>> | null>(null);
  useEffect(() => { settings.get().then((s) => setCfg(s as unknown as Cfg)).catch((e) => setMsg({ ok: false, text: errMsg(e) })); }, []);
  useEffect(() => {
    if (!cfg) return;
    let alive = true;
    const timer = window.setTimeout(() => settings.previewThresholds(cfg.thresholds).then((r) => { if (alive) setPreview(r); }).catch(() => undefined), 350);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [cfg?.thresholds.sure, cfg?.thresholds.margin, cfg?.thresholds.check]);
  if (!cfg) return msg ? <Notice>{msg.text}</Notice> : <Spinner />;
  const num = (k: 'sure' | 'margin' | 'check') => (
    <label className="text-xs font-semibold">{k}<input type="number" step="0.01" className={`${field} mt-1 w-full font-mono`} value={cfg.thresholds[k]} onChange={(e) => setCfg({ ...cfg, thresholds: { ...cfg.thresholds, [k]: Number(e.target.value) } })} /></label>
  );
  async function save() {
    if (!cfg) return;
    try {
      await settings.putThresholds(cfg.thresholds);
      await settings.putTolerance(cfg.tolerance);
      await settings.putPolicy({ auto_approve: cfg.auto_approve, retention_days: cfg.retention_days });
      setMsg({ ok: true, text: 'Saved. New scans use these values.' });
    } catch (e) { setMsg({ ok: false, text: errMsg(e) }); }
  }
  return (
    <section className="space-y-3 rounded-2xl border border-line bg-white p-4">
      <h2 className="text-sm font-bold">Confidence thresholds and tolerance</h2>
      <div className="grid grid-cols-3 gap-3">{num('sure')}{num('margin')}{num('check')}</div>
      <div className="grid grid-cols-2 gap-3">
        <label className="text-xs font-semibold">Tolerance %<input type="number" className={`${field} mt-1 w-full font-mono`} value={cfg.tolerance.pct} onChange={(e) => setCfg({ ...cfg, tolerance: { ...cfg.tolerance, pct: Number(e.target.value) } })} /></label>
        <label className="text-xs font-semibold">Tolerance units<input type="number" className={`${field} mt-1 w-full font-mono`} value={cfg.tolerance.units} onChange={(e) => setCfg({ ...cfg, tolerance: { ...cfg.tolerance, units: Number(e.target.value) } })} /></label>
      </div>
      <label className="flex items-center gap-2 text-sm font-semibold"><input type="checkbox" checked={cfg.auto_approve} onChange={(e) => setCfg({ ...cfg, auto_approve: e.target.checked })} />Auto-approve scans within tolerance</label>
      <label className="block max-w-xs text-xs font-semibold">Data retention (days)<input type="number" min={30} max={3650} value={cfg.retention_days} onChange={(e) => setCfg({ ...cfg, retention_days: Number(e.target.value) })} className={`${field} mt-1 w-full font-mono`} /><span className="mt-1 block font-normal text-muted">Approved scans and media follow this organization retention setting.</span></label>
      {preview && <div className="rounded-xl border border-line bg-cream p-3" aria-live="polite">
        <p className="text-xs font-bold">What would be flagged · last 20 processed scans ({preview.scans} scans, {preview.boxes} boxes)</p>
        <div className="mt-2 grid grid-cols-2 gap-3 text-[11px]"><div><p className="font-semibold text-muted">Current</p><p className="font-mono">{preview.current.SURE} sure · {preview.current.CHECK} check · {preview.current.UNKNOWN} unknown</p></div><div><p className="font-semibold text-muted">Proposed</p><p className="font-mono">{preview.proposed.SURE} sure · {preview.proposed.CHECK} check · {preview.proposed.UNKNOWN} unknown</p></div></div>
      </div>}
      {msg && <Notice tone={msg.ok ? 'brand' : 'danger'}>{msg.text}</Notice>}
      <button type="button" className={btnPrimary} onClick={save}>Save settings</button>
    </section>
  );
}

function Locations() {
  const [stores, setStores] = useState<Store[]>([]);
  const [shelves, setShelves] = useState<Shelf[]>([]);
  const [storeName, setStoreName] = useState('');
  const [shelfName, setShelfName] = useState('');
  const [storeId, setStoreId] = useState('');
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    const [st, sh] = await Promise.all([catalog.stores(), catalog.shelves()]);
    setStores(st); setShelves(sh); setStoreId((id) => st.some((x) => x.id === id) ? id : st[0]?.id ?? '');
  }, []);
  useEffect(() => { load().catch((e) => setError(errMsg(e))); }, [load]);
  async function addStore(e: FormEvent) { e.preventDefault(); setError(null); try { const s = await catalog.createStore(storeName.trim()); setStoreName(''); await load(); setStoreId(s.id); } catch (e) { setError(errMsg(e)); } }
  async function addShelf(e: FormEvent) { e.preventDefault(); setError(null); try { await catalog.createShelf(storeId, shelfName.trim()); setShelfName(''); await load(); } catch (e) { setError(errMsg(e)); } }
  return <section className="space-y-3 rounded-2xl border border-line bg-white p-4">
    <div><h2 className="text-sm font-bold">Stores and shelves</h2><p className="text-xs text-muted">Manage locations used when staff create scans.</p></div>
    {error && <Notice>{error}</Notice>}
    <form onSubmit={addStore} className="flex flex-wrap items-end gap-2"><label className="min-w-48 flex-1 text-xs font-semibold">New store<input required value={storeName} onChange={(e) => setStoreName(e.target.value)} className={`${field} mt-1 w-full`} /></label><button type="submit" className={btnSecondary}>Add store</button></form>
    <form onSubmit={addShelf} className="flex flex-wrap items-end gap-2"><label className="text-xs font-semibold">Store<select required value={storeId} onChange={(e) => setStoreId(e.target.value)} className={`${field} mt-1 block`}>{stores.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label><label className="min-w-48 flex-1 text-xs font-semibold">New shelf<input required value={shelfName} onChange={(e) => setShelfName(e.target.value)} className={`${field} mt-1 w-full`} /></label><button type="submit" disabled={!storeId} className={btnSecondary}>Add shelf</button></form>
    <ul className="grid gap-2 text-xs sm:grid-cols-2">{shelves.map((s) => <li key={s.id} className="rounded-xl bg-cream px-3 py-2"><b>{s.store_name}</b><span className="mx-2 text-muted">/</span>{s.name}</li>)}</ul>
  </section>;
}

export function TeamPage() {
  const [rows, setRows] = useState<AppUser[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [email, setEmail] = useState('');
  const [role, setRole] = useState<Role>('STAFF');
  const [info, setInfo] = useState<string | null>(null);
  const load = useCallback(() => { users.list().then(setRows).catch((e) => setError(errMsg(e))); }, []);
  useEffect(load, [load]);

  async function invite(e: FormEvent) {
    e.preventDefault(); setError(null); setInfo(null);
    try {
      const r = await users.invite({ email, role });
      setInfo(r.invite_token ? `Invite created. Share this link with ${email}: ${window.location.origin}/#/invite/${r.invite_token}` : `Invite emailed to ${email}.`);
      setEmail(''); load();
    } catch (x) { setError(errMsg(x)); }
  }
  const patch = (id: string, b: { role?: string; active?: boolean }) => users.patch(id, b).then(load).catch((x) => setError(errMsg(x)));

  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-bold tracking-tight">Team & settings</h1>
      <form onSubmit={invite} className="flex flex-wrap items-end gap-3 rounded-2xl border border-line bg-white p-4">
        <label className="min-w-48 flex-1 text-xs font-semibold">Invite by email<input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} className={`${field} mt-1 w-full`} /></label>
        <label className="text-xs font-semibold">Role<select value={role} onChange={(e) => setRole(e.target.value as Role)} className={`${field} mt-1 block`}>{ROLES.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}</select></label>
        <button type="submit" className={btnPrimary}>Send invite</button>
      </form>
      {error && <Notice>{error}</Notice>}{info && <Notice tone="brand"><span className="break-all">{info}</span></Notice>}
      {!rows && !error && <Spinner />}
      <div className="overflow-x-auto rounded-2xl border border-line bg-white">
        <table className="w-full text-left text-sm">
          <thead><tr className="text-[10px] uppercase tracking-wider text-muted"><th className="p-3">Name</th><th className="p-3">Role</th><th className="p-3">Status</th><th className="p-3" /></tr></thead>
          <tbody>{rows?.map((u) => (
            <tr key={u.id} className="border-t border-line">
              <td className="p-3"><b>{u.name}</b><span className="block text-xs text-muted">{u.email}</span></td>
              <td className="p-3"><select value={u.role} aria-label={`Role for ${u.name}`} onChange={(e) => patch(u.id, { role: e.target.value })} className={field}>{ROLES.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}</select></td>
              <td className="p-3 text-xs">{u.pending ? 'Invited' : u.active ? 'Active' : 'Deactivated'}</td>
              <td className="p-3 text-right"><button type="button" className={btnSecondary} onClick={() => patch(u.id, { active: !u.active })}>{u.active ? 'Deactivate' : 'Reactivate'}</button></td>
            </tr>))}
          </tbody>
        </table>
      </div>
      <Thresholds />
      <Locations />
    </div>
  );
}
