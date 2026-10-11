import { useEffect, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { auth } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import { setAccessToken } from '../../lib/http';
import { Logo, btnPrimary } from '../ui';
import { Notice, errMsg } from './shared';

const field = 'mt-1 w-full rounded-xl border border-line-strong bg-white px-3.5 py-2.5 text-sm outline-none focus:border-brand focus:ring-2 focus:ring-brand/20';

function Frame({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="grid min-h-screen place-items-center bg-paper px-4">
      <div className="w-full max-w-sm rounded-2xl border border-line bg-white p-7 shadow-card">
        <Logo />
        <h1 className="mt-5 text-xl font-bold tracking-tight">{title}</h1>
        {children}
      </div>
    </div>
  );
}

export function LoginPage() {
  const { login } = useAuth();
  const [mode, setMode] = useState<'login' | 'setup' | 'forgot'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [name, setName] = useState('');
  const [org, setOrg] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setSent(false);
    try {
      if (mode === 'forgot') {
        await auth.requestPasswordReset(email);
        setSent(true);
      } else if (mode === 'login') await login(email, password);
      else {
        // First-run: the first account becomes the Owner. Then sign in through the normal path so state is set once.
        await auth.register({ email, password, name, org_name: org || undefined });
        setAccessToken(null);
        await login(email, password);
      }
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Frame title={mode === 'login' ? 'Sign in' : mode === 'setup' ? 'Set up your organization' : 'Reset your password'}>
      <form onSubmit={submit} className="mt-5 space-y-4">
        {mode === 'setup' && (
          <>
            <label className="block text-xs font-semibold">Your name<input className={field} value={name} onChange={(e) => setName(e.target.value)} required autoComplete="name" /></label>
            <label className="block text-xs font-semibold">Organization<input className={field} value={org} onChange={(e) => setOrg(e.target.value)} placeholder="My store" /></label>
          </>
        )}
        <label className="block text-xs font-semibold">Email<input className={field} type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="username" /></label>
        {mode !== 'forgot' && <label className="block text-xs font-semibold">Password<input className={field} type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete={mode === 'login' ? 'current-password' : 'new-password'} /></label>}
        {error && <Notice>{error}</Notice>}
        {sent && <p role="status" className="rounded-xl bg-brand-mist p-3 text-xs text-ink">If an active account matches that email, a password reset link has been sent.</p>}
        <button type="submit" disabled={busy} className={`${btnPrimary} w-full`}>{busy ? 'Please wait…' : mode === 'login' ? 'Sign in' : mode === 'setup' ? 'Create Owner account' : 'Email reset link'}</button>
      </form>
      <div className="mt-4 flex flex-wrap gap-x-4 gap-y-2 text-xs font-semibold text-muted">
        {mode === 'login' && <button type="button" onClick={() => { setMode('forgot'); setError(null); setSent(false); }}>Forgot password?</button>}
        <button type="button" onClick={() => { setMode(mode === 'login' ? 'setup' : 'login'); setError(null); setSent(false); }}>
          {mode === 'setup' ? 'Back to sign in' : mode === 'forgot' ? 'Back to sign in' : 'First time here? Set up the Owner account'}
        </button>
      </div>
    </Frame>
  );
}

export function ResetPasswordPage({ token }: { token: string }) {
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError(null);
    try { await auth.resetPassword(token, password); setDone(true); }
    catch (err) { setError(errMsg(err)); }
    finally { setBusy(false); }
  }
  return <Frame title={done ? 'Password updated' : 'Choose a new password'}>
    {done ? <div className="mt-4 space-y-4"><p className="text-sm text-muted">Your password was changed and your other sessions were signed out.</p><a href="#/app" className={`${btnPrimary} block text-center`}>Return to sign in</a></div> :
      <form onSubmit={submit} className="mt-5 space-y-4">
        <label className="block text-xs font-semibold">New password<input className={field} type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="new-password" /></label>
        <p className="text-[11px] text-muted">Use at least 10 characters, including a letter and a number.</p>
        {error && <Notice>{error}</Notice>}
        <button type="submit" disabled={busy} className={`${btnPrimary} w-full`}>{busy ? 'Please wait…' : 'Update password'}</button>
      </form>}
  </Frame>;
}

export function InvitePage({ token }: { token: string }) {
  const { accept } = useAuth();
  const [info, setInfo] = useState<{ email: string; role: string } | null | 'bad'>(null);
  const [name, setName] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { auth.invite(token).then(setInfo).catch(() => setInfo('bad')); }, [token]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await accept(token, password, name || undefined);
      window.location.hash = '#/app';
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  if (info === 'bad') return <Frame title="Invite not valid"><p className="mt-3 text-sm text-muted">This invite link is invalid or has expired. Ask your Owner for a new one.</p></Frame>;
  if (!info) return <Frame title="Checking invite…"><span /></Frame>;
  return (
    <Frame title="Accept your invite">
      <p className="mt-2 text-sm text-muted">{info.email} · joining as <b>{info.role.toLowerCase()}</b></p>
      <form onSubmit={submit} className="mt-5 space-y-4">
        <label className="block text-xs font-semibold">Your name<input className={field} value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" /></label>
        <label className="block text-xs font-semibold">Choose a password<input className={field} type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="new-password" /></label>
        {error && <Notice>{error}</Notice>}
        <button type="submit" disabled={busy} className={`${btnPrimary} w-full`}>{busy ? 'Please wait…' : 'Create account'}</button>
      </form>
    </Frame>
  );
}
