/** One fetch wrapper for the whole app: base URL, bearer token, silent refresh on 401, plain-text or JSON errors. */

export const API_BASE: string = (import.meta.env.VITE_API_BASE ?? '/api').replace(/\/$/, '');

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

let accessToken: string | null = null;
let expiresAt = 0; // epoch ms; access tokens are short-lived
let onSessionLost: (() => void) | null = null;
let refreshing: Promise<boolean> | null = null;

export const setAccessToken = (t: string | null, expiresInSec = 0) => { accessToken = t; expiresAt = t ? Date.now() + expiresInSec * 1000 : 0; };
/** True when a token exists and has more than 30s left. */
export const tokenFresh = () => !!accessToken && Date.now() < expiresAt - 30_000;
export const getAccessToken = () => accessToken;
export const setSessionLostHandler = (fn: (() => void) | null) => { onSessionLost = fn; };

async function readError(res: Response): Promise<string> {
  const text = (await res.text().catch(() => '')).trim();
  if (!text) return `Request failed (${res.status})`;
  try {
    const j = JSON.parse(text);
    if (typeof j.detail === 'string') return j.detail;
  } catch { /* plain text body */ }
  return text.length > 300 ? `${text.slice(0, 300)}…` : text;
}

/** Exchange the httpOnly refresh cookie for a new access token. Concurrent callers share one request. */
export function refreshSession(): Promise<boolean> {
  if (!refreshing) {
    refreshing = fetch(`${API_BASE}/auth/refresh`, { method: 'POST', credentials: 'include' })
      .then(async (r) => {
        if (!r.ok) return false;
        const j = await r.json();
        setAccessToken(j.access_token, j.expires_in ?? 0);
        return true;
      })
      .catch(() => false)
      .finally(() => { refreshing = null; });
  }
  return refreshing;
}

export interface ReqOptions {
  method?: string;
  json?: unknown;
  form?: FormData;
  query?: Record<string, string | number | boolean | null | undefined>;
  auth?: boolean;
  signal?: AbortSignal;
}

function url(path: string, query?: ReqOptions['query']): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) if (v !== undefined && v !== null && v !== '') q.set(k, String(v));
  const s = q.toString();
  return `${API_BASE}${path}${s ? `?${s}` : ''}`;
}

async function send(path: string, o: ReqOptions): Promise<Response> {
  const headers: Record<string, string> = {};
  let body: BodyInit | undefined;
  if (o.json !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(o.json); }
  else if (o.form) body = o.form; // browser sets the multipart boundary
  if (o.auth !== false && accessToken) headers.Authorization = `Bearer ${accessToken}`;
  return fetch(url(path, o.query), { method: o.method ?? 'GET', headers, body, credentials: 'include', signal: o.signal });
}

export async function raw(path: string, o: ReqOptions = {}): Promise<Response> {
  let res = await send(path, o);
  if (res.status === 401 && o.auth !== false && !path.startsWith('/auth/')) {
    if (await refreshSession()) res = await send(path, o);
    else { onSessionLost?.(); throw new ApiError(401, 'Your session has expired. Please sign in again.'); }
  }
  if (!res.ok) throw new ApiError(res.status, await readError(res));
  return res;
}

export async function api<T = unknown>(path: string, o: ReqOptions = {}): Promise<T> {
  const res = await raw(path, o);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** Authenticated multipart upload with a callback once the browser has sent all photo bytes. */
export async function uploadApi<T>(path: string, form: FormData, onUploaded: () => void): Promise<T> {
  const sendXhr = () => new Promise<Response>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${API_BASE}${path}`);
    xhr.withCredentials = true;
    if (accessToken) xhr.setRequestHeader('Authorization', `Bearer ${accessToken}`);
    xhr.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable && event.loaded >= event.total) onUploaded();
    }, { once: true });
    xhr.addEventListener('load', () => resolve(new Response(xhr.responseText, {
      status: xhr.status, statusText: xhr.statusText, headers: { 'Content-Type': xhr.getResponseHeader('Content-Type') ?? 'text/plain' },
    })));
    xhr.addEventListener('error', () => reject(new ApiError(0, 'Photo upload failed. Check your connection and retry.')));
    xhr.addEventListener('abort', () => reject(new ApiError(0, 'Photo upload was cancelled.')));
    xhr.send(form);
  });
  let res = await sendXhr();
  if (res.status === 401) {
    if (await refreshSession()) res = await sendXhr();
    else { onSessionLost?.(); throw new ApiError(401, 'Your session has expired. Please sign in again.'); }
  }
  if (!res.ok) throw new ApiError(res.status, await readError(res));
  return (await res.json()) as T;
}

/** Download a file endpoint (CSV/PDF export) through the authenticated fetch. */
export async function download(path: string, filename: string, query?: ReqOptions['query']): Promise<void> {
  const res = await raw(path, { query });
  const blob = await res.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}
