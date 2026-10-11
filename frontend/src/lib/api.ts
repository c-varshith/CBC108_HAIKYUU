import { api, download, uploadApi } from './http';
import type {
  AppUser, Batch, CreditPage, ExpectedRow, IndexStatus, LiveFrameResult, Me, Notification, PatchAction, Product, ProductQuery, Progress, RecognitionPreview, ScanItem, ScanView, Shelf, Store, TokenResponse,
} from './contract';

export const auth = {
  login: (email: string, password: string) => api<TokenResponse>('/auth/login', { method: 'POST', json: { email, password }, auth: false }),
  requestPasswordReset: (email: string) => api<{ message: string }>('/auth/password/forgot', { method: 'POST', json: { email }, auth: false }),
  resetPassword: (token: string, password: string) => api<void>('/auth/password/reset', { method: 'POST', json: { token, password }, auth: false }),
  register: (b: { email: string; password: string; name: string; org_name?: string }) => api<TokenResponse>('/auth/register', { method: 'POST', json: b, auth: false }),
  me: () => api<Me>('/auth/me'),
  logout: () => api<void>('/auth/logout', { method: 'POST' }),
  logoutAll: () => api<void>('/auth/logout-all', { method: 'POST' }),
  invite: (token: string) => api<{ email: string; role: string }>(`/invites/${encodeURIComponent(token)}`, { auth: false }),
  accept: (token: string, password: string, name?: string) =>
    api<TokenResponse>(`/invites/${encodeURIComponent(token)}/accept`, { method: 'POST', json: { password, name }, auth: false }),
};

export const scans = {
  liveStatus: () => api<{ available: boolean; reason: string | null }>('/scans/live-status'),
  liveDetect: (frame: Blob, signal?: AbortSignal) => {
    const f = new FormData();
    f.set('image', frame, 'live-frame.jpg');
    return api<LiveFrameResult>('/scans/live-detect', { method: 'POST', form: f, signal });
  },
  list: (q: { status?: string; mine?: boolean; shelf_id?: string; person?: string; date_from?: string; date_to?: string; limit?: number } = {}) =>
    api<ScanItem[]>('/scans', { query: q }),
  get: (id: string) => api<ScanView>(`/scans/${id}`),
  progress: (id: string) => api<Progress>(`/scans/${id}/progress`),
  create: (shelfId: string, files: File[], device?: string, requestIds?: string[]) => {
    const f = new FormData();
    f.set('shelf_id', shelfId);
    if (device) f.set('device', device);
    const ids = requestIds ?? files.map(() => crypto.randomUUID());
    f.set('request_ids', JSON.stringify(ids));
    files.forEach((file) => f.append('files', file, file.name));
    return api<{ scan_id: string; scan_ids: string[]; status: string }>('/scans', { method: 'POST', form: f });
  },
  patch: (id: string, ids: string[], action: PatchAction, sku?: string) =>
    api<ScanView>(`/scans/${id}/detections`, { method: 'PATCH', json: { ids, action, sku } }),
  submit: (id: string, note?: string) => api<ScanView>(`/scans/${id}/submit`, { method: 'POST', json: { note: note || null } }),
  approve: (id: string) => api<ScanView>(`/scans/${id}/approve`, { method: 'POST' }),
  return: (id: string, note: string, pinned: string[]) => api<ScanView>(`/scans/${id}/return`, { method: 'POST', json: { note, pinned } }),
  reopen: (id: string, reason: string) => api<ScanView>(`/scans/${id}/reopen`, { method: 'POST', json: { reason } }),
  retry: (id: string) => api<Progress>(`/scans/${id}/retry`, { method: 'POST' }),
  remove: (id: string) => api<void>(`/scans/${id}`, { method: 'DELETE' }),
  comment: (id: string, text: string) => api<unknown>(`/scans/${id}/comments`, { method: 'POST', json: { text } }),
  export: (id: string, format: 'csv' | 'pdf') => download(`/scans/${id}/export`, `scan-${id.slice(0, 8)}.${format}`, { format }),
};

export const approvals = { inbox: () => api<ScanItem[]>('/approvals') };

export const catalog = {
  /** No options = everything (old behaviour). Pass limit/offset to page, `slim` to skip photo lists, `q` to search SKU/name/brand/barcode. */
  products: (includeInactive = false, o: ProductQuery = {}) => api<Product[]>('/catalog/products', { query: { include_inactive: includeInactive, ...o } }),
  product: (sku: string) => api<Product>(`/catalog/products/${encodeURIComponent(sku)}`),
  create: (b: { sku: string; name: string; min_qty?: number }) => api<Product>('/catalog/products', { method: 'POST', json: b }),
  patch: (sku: string, b: { name?: string; min_qty?: number; active?: boolean }) => api<Product>(`/catalog/products/${encodeURIComponent(sku)}`, { method: 'PATCH', json: b }),
  addReferences: (sku: string, files: File[], onUploaded?: () => void) => {
    const f = new FormData();
    files.forEach((file) => f.append('files', file, file.name));
    return onUploaded
      ? uploadApi<Product>(`/catalog/products/${encodeURIComponent(sku)}/references`, f, onUploaded)
      : api<Product>(`/catalog/products/${encodeURIComponent(sku)}/references`, { method: 'POST', form: f });
  },
  testRecognition: (sku: string, file: File) => {
    const f = new FormData(); f.set('file', file, file.name);
    return api<RecognitionPreview>(`/catalog/products/${encodeURIComponent(sku)}/test-recognition`, { method: 'POST', form: f });
  },
  /** Re-send the stored photos of a product the matching service never confirmed (status "failed"). */
  reindex: (sku: string) => api<Product>(`/catalog/products/${encodeURIComponent(sku)}/reindex`, { method: 'POST' }),
  /** Keep a corrected shelf crop as an extra reference photo (source "review_crop"). */
  addReferenceFromDetection: (sku: string, scanId: string, detectionId: string) =>
    api<Product>(`/catalog/products/${encodeURIComponent(sku)}/references/from-detection`, { method: 'POST', json: { scan_id: scanId, detection_id: detectionId } }),
  indexStatus: () => api<IndexStatus>('/catalog/index-status'),
  resync: () => api<{ batch_id: string; running: boolean }>('/catalog/resync', { method: 'POST' }),
  batch: (id: string) => api<Batch>(`/catalog/batches/${encodeURIComponent(id)}`),
  batches: (source?: string) => api<Batch[]>('/catalog/batches', { query: { source } }),
  credits: (o: { q?: string; limit?: number; offset?: number } = {}) => api<CreditPage>('/catalog/credits', { query: o }),
  removeReference: (sku: string, refId: string) => api<void>(`/catalog/products/${encodeURIComponent(sku)}/references/${refId}`, { method: 'DELETE' }),
  requests: (status?: string) => api<{ id: string; name: string; status: string; note: string | null; requested_by: { id: string; name: string }; created_at: string }[]>('/catalog/product-requests', { query: { status } }),
  decide: (id: string, decision: 'APPROVED' | 'REJECTED', note?: string) => api<unknown>(`/catalog/product-requests/${id}/decision`, { method: 'POST', json: { decision, note: note || null } }),
  request: (name: string, note?: string) => api<unknown>('/catalog/product-requests', { method: 'POST', json: { name, note } }),
  shelves: () => api<Shelf[]>('/shelves'),
  stores: () => api<Store[]>('/stores'),
  createStore: (name: string) => api<Store>('/stores', { method: 'POST', json: { name } }),
  createShelf: (store_id: string, name: string) => api<Shelf>('/shelves', { method: 'POST', json: { store_id, name } }),
};

export const stock = {
  expected: (shelfId?: string) => api<ExpectedRow[]>('/wms/expected', { query: { shelf_id: shelfId } }),
  set: (shelf_id: string, sku: string, expected_qty: number) => api<ExpectedRow>('/wms/expected', { method: 'PUT', json: { shelf_id, sku, expected_qty } }),
};

export const users = {
  list: () => api<AppUser[]>('/users'),
  invite: (b: { email: string; role: string; name?: string }) => api<AppUser & { invite_token?: string }>('/users/invite', { method: 'POST', json: b }),
  patch: (id: string, b: { role?: string; active?: boolean; name?: string }) => api<AppUser>(`/users/${id}`, { method: 'PATCH', json: b }),
};

export const notifications = {
  list: () => api<{ unread: number; items: Notification[] }>('/notifications'),
  readAll: () => api<void>('/notifications/read', { method: 'POST', json: { all: true } }),
};

export const settings = {
  get: () => api<Record<string, unknown>>('/settings'),
  putThresholds: (b: { sure: number; margin: number; check: number }) => api('/settings/thresholds', { method: 'PUT', json: b }),
  putTolerance: (b: { pct: number; units: number }) => api('/settings/tolerance', { method: 'PUT', json: b }),
  putPolicy: (b: { auto_approve?: boolean; retention_days?: number }) => api('/settings/approval-policy', { method: 'PUT', json: b }),
  previewThresholds: (thresholds: { sure: number; margin: number; check: number }) =>
    api<{ scans: number; boxes: number; current: Record<'SURE' | 'CHECK' | 'UNKNOWN', number>; proposed: Record<'SURE' | 'CHECK' | 'UNKNOWN', number> }>(
      '/settings/thresholds/preview', { method: 'POST', json: { thresholds } }),
};

export const reports = {
  summary: () => api<{ shrinkage_trend: number[]; accuracy: number; short_skus: number; surplus_skus: number; total_shrinkage: number }>('/reports/summary'),
  exportLowStock: () => download('/reports/low-stock/export', 'low-stock.csv'),
  lowStock: () => api<{ sku: string; name: string; on_hand: number; min_qty: number; reorder: number }[]>('/alerts/low-stock'),
};

export const audit = {
  list: (q: { q?: string; action?: string; scan_id?: string; limit?: number } = {}) =>
    api<{ id: string; at: string; actor: string; action: string; entity: string; scan_id: string | null; before: unknown; after: unknown }[]>('/audit-log', { query: q }),
  export: () => download('/audit-log/export', 'audit-log.csv'),
};

/** Server-sent progress. EventSource cannot send headers, so the access token goes in the query string (backend allows it on this route only). */
export function watchScan(id: string, token: string, onProgress: (p: Progress) => void, onError: () => void): () => void {
  const es = new EventSource(`${(import.meta.env.VITE_API_BASE ?? '/api').replace(/\/$/, '')}/scans/${id}/events?token=${encodeURIComponent(token)}`);
  es.addEventListener('progress', (e) => onProgress(JSON.parse((e as MessageEvent).data) as Progress));
  es.onerror = () => { es.close(); onError(); };
  return () => es.close();
}
