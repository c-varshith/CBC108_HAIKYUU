import { Bell, Boxes, Home, LogOut, ScanLine, ScrollText, Users, BarChart3, Warehouse, ListChecks, Moon, Sun, ZapOff, Command, CircleHelp, GitCompareArrows, Copyright, ChevronDown, Settings } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { notifications } from '../../lib/api';
import { ROLE_LABEL, useAuth } from '../../lib/auth';
import type { Cap } from '../../lib/auth';
import type { Notification, Role } from '../../lib/contract';
import { cn } from '../../utils/cn';
import { queuedUploads, removeQueuedUpload } from '../../lib/uploadQueue';
import { scans } from '../../lib/api';
import { Logo, btnPrimary } from '../ui';
import { AuditPage } from './Audit';
import { CatalogPage } from './Catalog';
import { CreditsPage } from './Credits';
import { HomePage } from './Home';
import { InventoryPage } from './Inventory';
import { InvitePage, LoginPage } from './Login';
import { ReportsPage } from './Reports';
import { ScanDetail } from './ScanDetail';
import { ScanNew } from './ScanNew';
import { ScansList } from './ScansList';
import { TeamPage } from './Team';
import { ScanCompare } from './ScanCompare';
import { InventoryAssistant } from './InventoryAssistant';
import { Spinner, go, fmtDate } from './shared';

type Item = { path: string; label: string; icon: typeof Home; cap?: Cap };
const THEME_STORAGE_VERSION = '2';

function initialWorkspaceTheme(): 'light' | 'dark' {
  if (localStorage.getItem('cx.theme-version') !== THEME_STORAGE_VERSION) return 'light';
  return localStorage.getItem('cx.theme') === 'dark' ? 'dark' : 'light';
}

/** Role-specific navigation (spec 1.7). Server still enforces; this only decides what to show. */
const NAV: Record<Role, Item[]> = {
  STAFF: [{ path: 'inventory', label: 'Inventory', icon: Boxes }, { path: 'scans', label: 'My scans', icon: ListChecks }, { path: 'compare', label: 'Compare', icon: GitCompareArrows }, { path: 'catalog', label: 'Catalog', icon: Warehouse }, { path: 'credits', label: 'Image credits', icon: Copyright }],
  MANAGER: [{ path: '', label: 'Home', icon: Home }, { path: 'inventory', label: 'Inventory', icon: Boxes }, { path: 'scans', label: 'Scans & approvals', icon: ListChecks }, { path: 'compare', label: 'Compare', icon: GitCompareArrows }, { path: 'catalog', label: 'Catalog & stock', icon: Warehouse }, { path: 'reports', label: 'Reports', icon: BarChart3 }, { path: 'audit', label: 'Audit', icon: ScrollText }, { path: 'credits', label: 'Image credits', icon: Copyright }],
  OWNER: [{ path: '', label: 'Home', icon: Home }, { path: 'inventory', label: 'Inventory', icon: Boxes }, { path: 'scans', label: 'Scans & approvals', icon: ListChecks }, { path: 'compare', label: 'Compare', icon: GitCompareArrows }, { path: 'catalog', label: 'Catalog & stock', icon: Warehouse }, { path: 'reports', label: 'Reports', icon: BarChart3 }, { path: 'team', label: 'Team & settings', icon: Users }, { path: 'audit', label: 'Audit', icon: ScrollText }, { path: 'credits', label: 'Image credits', icon: Copyright }],
  AUDITOR: [{ path: '', label: 'Home', icon: Home }, { path: 'inventory', label: 'Inventory', icon: Boxes }, { path: 'scans', label: 'Scans', icon: ListChecks }, { path: 'compare', label: 'Compare', icon: GitCompareArrows }, { path: 'reports', label: 'Reports', icon: BarChart3 }, { path: 'audit', label: 'Audit', icon: ScrollText }, { path: 'credits', label: 'Image credits', icon: Copyright }],
};
const LANDING: Record<Role, string> = { STAFF: 'scan', MANAGER: '', OWNER: '', AUDITOR: '' };

export function appPath(hash: string): string {
  return hash.replace(/^#\/app\/?/, '').replace(/\/+$/, '');
}

function Bell_() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<{ unread: number; items: Notification[] }>({ unread: 0, items: [] });
  const load = useCallback(() => { notifications.list().then(setData).catch(() => undefined); }, []);
  useEffect(() => { load(); const t = window.setInterval(load, 30000); return () => window.clearInterval(t); }, [load]);
  return (
    <div className="relative">
      <button type="button" aria-label={`Notifications, ${data.unread} unread`} onClick={() => { setOpen((o) => !o); load(); }} className="relative grid h-9 w-9 place-items-center rounded-lg hover:bg-cream">
        <Bell className="h-[18px] w-[18px]" />
        {data.unread > 0 && <span className="absolute -right-0.5 -top-0.5 grid min-w-4 place-items-center rounded-full bg-danger px-1 font-mono text-[9px] font-bold text-white">{data.unread}</span>}
      </button>
      {open && (
        <div className="absolute right-0 z-50 mt-2 w-80 max-w-[90vw] rounded-xl border border-line bg-white p-2 shadow-pop">
          <div className="flex items-center justify-between px-2 py-1.5">
            <p className="text-xs font-bold">Notifications</p>
            <button type="button" className="text-[11px] font-semibold text-brand" onClick={() => notifications.readAll().then(load)}>Mark all read</button>
          </div>
          <ul className="max-h-80 overflow-auto">
            {data.items.length === 0 && <li className="px-2 py-4 text-center text-xs text-muted">Nothing yet.</li>}
            {data.items.map((n) => (
              <li key={n.id}>
                <button type="button" onClick={() => { setOpen(false); if (n.scan_id) go(`scans/${n.scan_id}`); }} className={cn('w-full rounded-lg px-2 py-2 text-left text-xs hover:bg-cream', !n.read && 'bg-brand-mist')}>
                  <span className="block font-medium">{n.text}</span>
                  <span className="text-[10px] text-muted">{fmtDate(n.at)}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function Shell({ path, children }: { path: string; children: ReactNode }) {
  const { user, logout, can } = useAuth();
  const [theme, setTheme] = useState(initialWorkspaceTheme);
  const [reduceEffects, setReduceEffects] = useState(() => localStorage.getItem('cx.reduce-effects') === 'true');
  const [palette, setPalette] = useState(false);
  const [help, setHelp] = useState(false);
  const [command, setCommand] = useState('');
  const [queueCount, setQueueCount] = useState(0);
  const [online, setOnline] = useState(navigator.onLine);
  const [queueMessage, setQueueMessage] = useState('');
  const [profileOpen, setProfileOpen] = useState(false);
  const profileRef = useRef<HTMLDivElement>(null);
  const items = user ? NAV[user.role] : [];
  const active = (p: string) => (p === '' ? path === '' : path === p || path.startsWith(`${p}/`));
  useEffect(() => {
    localStorage.setItem('cx.theme', theme);
    localStorage.setItem('cx.theme-version', THEME_STORAGE_VERSION);
    localStorage.setItem('cx.reduce-effects', String(reduceEffects));
  }, [theme, reduceEffects]);
  useEffect(() => {
    const keys = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      const typing = target.closest('input, textarea, select, [contenteditable="true"]');
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); setPalette((x) => !x); setHelp(false); }
      else if (!typing && e.key === '?') { e.preventDefault(); setHelp((x) => !x); setPalette(false); }
      else if (e.key === 'Escape') { setPalette(false); setHelp(false); }
    };
    window.addEventListener('keydown', keys); return () => window.removeEventListener('keydown', keys);
  }, []);
  useEffect(() => {
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (profileRef.current && !profileRef.current.contains(event.target as Node)) setProfileOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') setProfileOpen(false); };
    document.addEventListener('mousedown', closeOnOutsideClick);
    document.addEventListener('keydown', closeOnEscape);
    return () => { document.removeEventListener('mousedown', closeOnOutsideClick); document.removeEventListener('keydown', closeOnEscape); };
  }, []);
  const refreshQueue = useCallback(async () => { try { setQueueCount((await queuedUploads()).length); } catch { setQueueCount(0); } }, []);
  const flushQueue = useCallback(async () => {
    if (!navigator.onLine || !user) { await refreshQueue(); return; }
    try {
      const queued = await queuedUploads();
      let uploaded = 0;
      for (const item of queued) {
        const file = new File([item.blob], item.name, { type: item.type, lastModified: item.lastModified });
        await scans.create(item.shelfId, [file], 'Offline upload queue', [item.id]);
        await removeQueuedUpload(item.id);
        uploaded += 1;
      }
      await refreshQueue();
      if (uploaded) setQueueMessage(`${uploaded} offline photo${uploaded === 1 ? '' : 's'} uploaded and counting has started.`);
    } catch { setQueueMessage('Queued photos are saved on this device and will retry when the connection is ready.'); await refreshQueue(); }
  }, [refreshQueue, user]);
  useEffect(() => {
    const update = () => { setOnline(navigator.onLine); if (navigator.onLine) void flushQueue(); else void refreshQueue(); };
    const onQueue = () => { void refreshQueue(); };
    window.addEventListener('online', update); window.addEventListener('offline', update); window.addEventListener('cx:queue-change', onQueue);
    void refreshQueue(); if (navigator.onLine) void flushQueue();
    return () => { window.removeEventListener('online', update); window.removeEventListener('offline', update); window.removeEventListener('cx:queue-change', onQueue); };
  }, [flushQueue, refreshQueue]);
  const commands = [...items, ...(can('scan.capture') ? [{ path: 'scan', label: 'Scan', icon: ScanLine }] : [])]
    .filter((i) => !i.cap).filter((i) => `${i.label} ${i.path}`.toLowerCase().includes(command.toLowerCase()));
  if (!user) return null;
  return (
    <div className="workspace-shell min-h-screen bg-paper md:flex" data-theme={theme} data-reduce-effects={reduceEffects}>
      <aside className="workspace-sidebar workspace-side-rail hidden w-[286px] shrink-0 flex-col border-r border-line bg-white p-4 md:flex">
        <a href="#/app" className="workspace-brand px-1"><Logo /></a>
        <nav aria-label="Main" className="mt-6 flex-1 space-y-1">
          {items.map((i) => (
            <button key={i.label} type="button" onClick={() => go(i.path)} aria-current={active(i.path) ? 'page' : undefined}
              className={cn('workspace-nav-item flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-semibold', active(i.path) ? 'bg-ink text-white' : 'text-muted hover:bg-cream hover:text-ink')} title={i.label}>
              <i.icon className="h-[18px] w-[18px]" /><span className="nav-label">{i.label}</span>
            </button>
          ))}
        </nav>
      </aside>
      <div className="min-w-0 flex-1 pb-32 md:pb-24">
        <header className="workspace-topbar flex items-center justify-between border-b border-line bg-white/80 px-4 py-4 backdrop-blur md:justify-end">
          <span className="md:hidden"><Logo compact /></span>
          <div className="flex items-center gap-1">
            <button type="button" onClick={() => setPalette(true)} aria-label="Open command palette" title="Command palette · Ctrl+K" className="hidden h-9 items-center gap-1.5 rounded-lg px-2 text-xs text-muted hover:bg-cream lg:flex"><Command className="h-4 w-4" /><kbd>Ctrl K</kbd></button>
            <button type="button" onClick={() => setTheme((v) => v === 'dark' ? 'light' : 'dark')} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`} title="Toggle theme" className="grid h-9 w-9 place-items-center rounded-lg hover:bg-cream">{theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}</button>
            <button type="button" onClick={() => setReduceEffects((v) => !v)} aria-pressed={reduceEffects} aria-label="Reduce effects" title="Reduce effects" className={cn('grid h-9 w-9 place-items-center rounded-lg hover:bg-cream', reduceEffects && 'bg-brand-mist text-brand')}><ZapOff className="h-4 w-4" /></button>
            <button type="button" onClick={() => setHelp(true)} aria-label="Keyboard shortcuts" title="Keyboard shortcuts · ?" className="grid h-9 w-9 place-items-center rounded-lg hover:bg-cream"><CircleHelp className="h-4 w-4" /></button>
            <Bell_ />
            <div className="relative ml-1" ref={profileRef}>
              <button type="button" aria-haspopup="menu" aria-expanded={profileOpen} onClick={() => setProfileOpen((open) => !open)} className="flex h-10 items-center gap-2 rounded-xl px-2 hover:bg-cream focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand">
                <span className="grid h-8 w-8 place-items-center rounded-full bg-brand-mist text-xs font-bold text-brand">{user.name.trim().split(/\s+/).map((part) => part[0]).slice(0, 2).join('').toUpperCase()}</span>
                <span className="hidden max-w-36 text-left sm:block"><span className="block truncate text-xs font-semibold text-ink">{user.name}</span><span className="block text-[10px] text-muted">{ROLE_LABEL[user.role]}</span></span>
                <ChevronDown className="hidden h-4 w-4 text-muted sm:block" />
              </button>
              {profileOpen && <div role="menu" aria-label="Account menu" className="absolute right-0 z-50 mt-2 w-64 overflow-hidden rounded-xl border border-line bg-white p-1.5 shadow-pop">
                <div className="border-b border-line px-3 py-2.5">
                  <p className="truncate text-sm font-semibold">{user.name}</p>
                  <p className="truncate text-xs text-muted">{user.email}</p>
                  <p className="mt-1 text-[10px] font-semibold uppercase tracking-wide text-muted">{ROLE_LABEL[user.role]}</p>
                </div>
                {can('users.manage') && <button type="button" role="menuitem" onClick={() => { setProfileOpen(false); go('team'); }} className="flex w-full items-center gap-2 rounded-lg px-3 py-2.5 text-left text-sm font-medium text-ink hover:bg-cream"><Settings className="h-4 w-4 text-muted" />Settings</button>}
                <button type="button" role="menuitem" onClick={() => { setProfileOpen(false); void logout(); }} className="flex w-full items-center gap-2 rounded-lg px-3 py-2.5 text-left text-sm font-semibold text-danger hover:bg-danger/10"><LogOut className="h-4 w-4" />Sign out</button>
              </div>}
            </div>
          </div>
        </header>
        {(!online || queueCount > 0 || queueMessage) && <div role="status" className="border-b border-line bg-brand-mist px-4 py-2 text-xs font-medium text-ink">
          {!online ? `Offline · ${queueCount} photo${queueCount === 1 ? '' : 's'} queued on this device. They’ll upload when you reconnect.` : queueCount ? `${queueCount} queued photo${queueCount === 1 ? '' : 's'} waiting to upload.` : queueMessage}
        </div>}
        <main className="mx-auto max-w-[1260px] px-4 py-6 md:px-10 md:py-7">{children}</main>
      </div>
      {can('scan.capture') && <button type="button" onClick={() => go('scan')} aria-current={active('scan') ? 'page' : undefined} className={`${btnPrimary} fixed bottom-20 left-1/2 z-40 -translate-x-1/2 !px-7 !py-3.5 text-base shadow-[0_14px_34px_-12px_rgba(12,122,69,0.75)] md:bottom-6 md:left-[calc(50%_+_36px)] lg:left-[calc(50%_+_143px)]`}>
        <ScanLine className="h-5 w-5" />Scan
      </button>}
      {can('inventory.view') && <InventoryAssistant />}
      <nav aria-label="Main" className="workspace-mobile-nav fixed inset-x-3 bottom-3 z-40 flex justify-around rounded-2xl border border-line bg-white/95 p-1.5 shadow-pop backdrop-blur md:hidden" style={{ paddingBottom: 'max(0.375rem, env(safe-area-inset-bottom))' }}>
        {items.slice(0, 5).map((i) => (
          <button key={i.label} type="button" onClick={() => go(i.path)} aria-current={active(i.path) ? 'page' : undefined}
            className={cn('flex min-h-11 min-w-11 flex-1 flex-col items-center justify-center gap-0.5 rounded-xl px-1 text-[10px] font-semibold', active(i.path) ? 'bg-ink text-white' : 'text-muted')}>
            <i.icon className="h-5 w-5" />{i.label.split(' ')[0]}
          </button>
        ))}
      </nav>
      {palette && <div role="dialog" aria-modal="true" aria-label="Command palette" className="fixed inset-0 z-[70] flex items-start justify-center bg-slate-950/60 p-4 pt-[12vh]" onMouseDown={(e) => { if (e.target === e.currentTarget) setPalette(false); }}>
        <div className="w-full max-w-xl overflow-hidden rounded-2xl border border-line bg-white shadow-pop">
          <input autoFocus value={command} onChange={(e) => setCommand(e.target.value)} placeholder="Type a page or action…" aria-label="Search commands" className="w-full border-b border-line bg-transparent px-5 py-4 text-sm outline-none" />
          <ul className="max-h-[55vh] overflow-auto p-2">{commands.map((i) => <li key={i.path}><button type="button" onClick={() => { go(i.path); setPalette(false); setCommand(''); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-3 text-left text-sm hover:bg-cream"><i.icon className="h-4 w-4 text-brand" />{i.label}</button></li>)}{commands.length === 0 && <li className="p-4 text-center text-xs text-muted">No matching pages for this role.</li>}</ul>
          <p className="border-t border-line px-4 py-2 text-[10px] text-muted">Only pages available to your role are listed. Esc closes.</p>
        </div>
      </div>}
      {help && <div role="dialog" aria-modal="true" aria-label="Keyboard shortcuts" className="fixed inset-0 z-[70] grid place-items-center bg-slate-950/60 p-4" onMouseDown={(e) => { if (e.target === e.currentTarget) setHelp(false); }}>
        <div className="w-full max-w-lg rounded-2xl border border-line bg-white p-5 shadow-pop"><div className="flex items-center justify-between"><h2 className="text-lg font-bold">Keyboard shortcuts</h2><button type="button" onClick={() => setHelp(false)} className="rounded-lg px-3 py-2 text-sm hover:bg-cream">Close</button></div>
          <dl className="mt-4 grid grid-cols-[1fr_auto] gap-x-6 gap-y-3 text-sm"><dt>Open command palette</dt><dd><kbd>Ctrl K</kbd></dd><dt>Show this guide</dt><dd><kbd>?</kbd></dd><dt>Select a box</dt><dd><kbd>Click</kbd></dd><dt>Multi-select boxes</dt><dd><kbd>Shift click / drag</kbd></dd><dt>Pan image</dt><dd><kbd>Space + drag</kbd></dd><dt>Zoom image</dt><dd><kbd>Wheel / pinch</kbd></dd><dt>Undo a review action</dt><dd><kbd>Ctrl Z</kbd></dd></dl>
        </div>
      </div>}
    </div>
  );
}

function Redirect({ to }: { to: string }) {
  useEffect(() => { go(to); }, [to]);
  return null;
}

function NoPermission() {
  return <div className="mx-auto max-w-md rounded-2xl border border-line bg-white p-8 text-center"><h2 className="text-lg font-bold">You don't have access to this page</h2><p className="mt-2 text-sm text-muted">Your role can't open it. Ask an Owner if you need access.</p></div>;
}

export function Workspace({ hash }: { hash: string }) {
  const { user, ready, can } = useAuth();
  const invite = hash.match(/^#\/invite\/(.+)$/);
  if (invite) return <InvitePage token={decodeURIComponent(invite[1])} />;
  if (!ready) return <div className="grid min-h-screen place-items-center"><Spinner /></div>;
  if (!user) return <LoginPage />;

  const path = appPath(hash);
  const nav = NAV[user.role];
  const first = path.split('/')[0];
  // role landing page, and route guard: a path not in this role's nav (other than scan detail) is no-permission
  if (path === '' && LANDING[user.role] !== '') return <Redirect to={LANDING[user.role]} />;
  const allowed = first === 'scans' || (first === 'inventory' && can('inventory.view')) || (first === 'scan' && can('scan.capture'))
    || (first === 'approvals' && can('scan.approve')) || nav.some((i) => i.path === first);

  let page: ReactNode;
  if (!allowed) page = <NoPermission />;
  else if (first === '') page = <HomePage />;
  else if (first === 'scan') page = <ScanNew />;
  else if (first === 'scans') page = path.split('/')[1] ? <ScanDetail key={path} id={path.split('/')[1]} /> : <ScansList key="scans" />;
  else if (first === 'compare') page = <ScanCompare ids={path.split('/').slice(1)} />;
  else if (first === 'approvals') page = <ScansList key="approvals" initialView="approvals" />;
  else if (first === 'inventory') page = <InventoryPage />;
  else if (first === 'catalog') page = <CatalogPage />;
  else if (first === 'reports') page = <ReportsPage />;
  else if (first === 'team') page = <TeamPage />;
  else if (first === 'audit') page = <AuditPage />;
  else if (first === 'credits') page = <CreditsPage />;
  else page = <NoPermission />;
  return <Shell path={path}>{page}</Shell>;
}
