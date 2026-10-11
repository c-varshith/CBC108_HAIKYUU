import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { auth } from './api';
import { getAccessToken, refreshSession, setAccessToken, setSessionLostHandler, tokenFresh } from './http';
import type { Me, Role, TokenResponse } from './contract';

/** Client mirror of backend/app/permissions.py. UI hides/disables only: the server stays the authority (and ScanView.permissions wins per scan). */
const CAPS: Record<string, Role[]> = {
  'scan.capture': ['OWNER', 'MANAGER', 'STAFF', 'AUDITOR'],
  'scan.view': ['OWNER', 'MANAGER', 'STAFF', 'AUDITOR'],
  'scan.review': ['OWNER', 'MANAGER', 'STAFF', 'AUDITOR'],
  'scan.submit': ['OWNER', 'MANAGER', 'STAFF', 'AUDITOR'],
  'scan.approve': ['OWNER', 'MANAGER'],
  'scan.reopen': ['OWNER', 'MANAGER'],
  'scan.delete': ['OWNER', 'STAFF'],
  'inventory.view': ['OWNER', 'MANAGER', 'STAFF', 'AUDITOR'],
  'catalog.write': ['OWNER', 'MANAGER'],
  'catalog.request': ['STAFF'],
  'catalog.resync': ['OWNER'],
  'stock.write': ['OWNER', 'MANAGER'],
  'alerts.write': ['OWNER', 'MANAGER'],
  'settings.thresholds': ['OWNER'],
  'users.manage': ['OWNER'],
  'audit.view': ['OWNER', 'MANAGER', 'AUDITOR'],
  'reports.export': ['OWNER', 'MANAGER', 'AUDITOR'],
};
export type Cap = keyof typeof CAPS;

/** UI convenience only; API permissions remain authoritative. */
export function roleCan(role: Role, cap: Cap): boolean {
  return CAPS[cap]?.includes(role) ?? false;
}

interface AuthValue {
  user: Me | null;
  ready: boolean; // false until the first silent refresh finished
  login: (email: string, password: string) => Promise<void>;
  signup: (b: { email: string; password: string; name: string }) => Promise<void>;
  accept: (token: string, password: string, name?: string) => Promise<void>;
  logout: () => Promise<void>;
  can: (cap: Cap) => boolean;
  token: () => Promise<string | null>;
}

const Ctx = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<Me | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setSessionLostHandler(() => { setAccessToken(null); setUser(null); });
    let alive = true;
    (async () => {
      // Page load: the access token lives only in memory, so trade the refresh cookie for a fresh one.
      if (await refreshSession()) {
        try { const me = await auth.me(); if (alive) setUser(me); } catch { /* not signed in */ }
      }
      if (alive) setReady(true);
    })();
    return () => { alive = false; setSessionLostHandler(null); };
  }, []);

  const adopt = useCallback((t: TokenResponse) => { setAccessToken(t.access_token, t.expires_in); setUser(t.user); }, []);

  const value = useMemo<AuthValue>(() => ({
    user,
    ready,
    login: async (email, password) => adopt(await auth.login(email, password)),
    signup: async (b) => adopt(await auth.signup(b)),
    accept: async (token, password, name) => adopt(await auth.accept(token, password, name)),
    logout: async () => {
      try { await auth.logout(); } catch { /* cookie may already be gone */ }
      setAccessToken(null);
      setUser(null);
    },
    can: (cap) => !!user && roleCan(user.role, cap),
    // SSE needs a token string: make sure it is fresh (access tokens live 15 minutes).
    token: async () => (tokenFresh() ? getAccessToken() : (await refreshSession()) ? getAccessToken() : null),
  }), [user, ready, adopt]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthValue {
  const v = useContext(Ctx);
  if (!v) throw new Error('useAuth must be used inside <AuthProvider>');
  return v;
}

export const ROLE_LABEL: Record<Role, string> = { OWNER: 'Owner', MANAGER: 'Manager', STAFF: 'Staff', AUDITOR: 'Auditor' };
