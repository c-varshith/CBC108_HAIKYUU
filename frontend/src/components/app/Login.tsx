import { useEffect, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { auth } from '../../lib/api';
import { useAuth } from '../../lib/auth';
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
  const { login, signup } = useAuth();
  const [mode, setMode] = useState<'login' | 'signup' | 'forgot'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [name, setName] = useState('');
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
        await signup({ email, password, name });
      }
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Frame title={mode === 'login' ? 'Sign in' : mode === 'signup' ? 'Create your account' : 'Reset your password'}>
      <form onSubmit={submit} className="mt-5 space-y-4">
        {mode === 'signup' && (
          <label className="block text-xs font-semibold">Your name<input className={field} value={name} onChange={(e) => setName(e.target.value)} required autoComplete="name" /></label>
        )}
        <label className="block text-xs font-semibold">Email<input className={field} type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="username" /></label>
        {mode !== 'forgot' && <label className="block text-xs font-semibold">Password<input className={field} type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete={mode === 'login' ? 'current-password' : 'new-password'} /></label>}
        {mode === 'signup' && <p className="-mt-2 text-[11px] text-muted">Use at least 10 characters, including a letter and a number.</p>}
        {error && <Notice>{error}</Notice>}
        {sent && <p role="status" className="rounded-xl bg-brand-mist p-3 text-xs text-ink">If an active account matches that email, a password reset link has been sent.</p>}
        {mode === 'signup' && <p className="text-[11px] leading-relaxed text-muted">This creates a new supermarket workspace and makes you its Owner. To join an existing supermarket as Staff, Manager, or Auditor, accept an invite from its Owner.</p>}
        <button type="submit" disabled={busy} className={`${btnPrimary} w-full`}>{busy ? 'Please wait…' : mode === 'login' ? 'Sign in' : mode === 'signup' ? 'Create Owner account' : 'Email reset link'}</button>
      </form>
      <div className="mt-4 flex flex-wrap gap-x-4 gap-y-2 text-xs font-semibold text-muted">
        {mode === 'login' && <button type="button" onClick={() => { setMode('forgot'); setError(null); setSent(false); }}>Forgot password?</button>}
        <button type="button" onClick={() => { setMode(mode === 'login' ? 'signup' : 'login'); setError(null); setSent(false); }}>
          {mode === 'signup' ? 'Already have an account? Sign in' : mode === 'forgot' ? 'Back to sign in' : 'Create an account'}
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
  const [info, setInfo] = useState<{ email: string; role: string; existing_account?: boolean } | null | 'bad'>(null);
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
        {!info.existing_account && <label className="block text-xs font-semibold">Your name<input className={field} value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" /></label>}
        <label className="block text-xs font-semibold">{info.existing_account ? 'Current password' : 'Choose a password'}<input className={field} type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete={info.existing_account ? 'current-password' : 'new-password'} /></label>
        {info.existing_account && <p className="text-[11px] text-muted">This account already exists in another workspace. Confirm your password to move it into this supermarket. Its old workspace data remains separate.</p>}
        {error && <Notice>{error}</Notice>}
        <button type="submit" disabled={busy} className={`${btnPrimary} w-full`}>{busy ? 'Please wait…' : 'Create account'}</button>
      </form>
    </Frame>
  );
}
