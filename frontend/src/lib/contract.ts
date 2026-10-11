/** Shapes the backend sends (spec 1.9 plus list/approval/catalog views). Keep in step with backend/app/domain/scans.py. */

export type Role = 'OWNER' | 'MANAGER' | 'STAFF' | 'AUDITOR';
export type DetState = 'SURE' | 'CHECK' | 'UNKNOWN' | 'CORRECTED';
export type ScanStatus =
  | 'DRAFT' | 'UPLOADED' | 'PROCESSING' | 'NEEDS_REVIEW' | 'IN_REVIEW' | 'AUTO_READY'
  | 'SUBMITTED' | 'APPROVED' | 'RETURNED' | 'REOPENED' | 'FAILED' | 'ARCHIVED';

export interface UserRef { id: string; name: string; role?: Role }
export interface Me { id: string; name: string; email: string; role: Role }
export interface TokenResponse { access_token: string; expires_in: number; user: Me }

export interface Match { sku: string; name?: string; score: number }
export interface Detection {
  id: string;
  bbox: [number, number, number, number]; // pixels of the ORIGINAL image
  state: DetState;
  top_matches: Match[];
  assigned_sku: string | null;
  det_confidence: number;
  corrected_by: string | null;
  deleted?: boolean;
  needs_review?: boolean;
  accepted_unknown?: boolean;
  flags?: string[];
}
export interface LiveEstimate { sku: string | null; name: string; quantity: number; needs_review: number }
export interface LiveFrameResult {
  width: number; height: number;
  detections: Pick<Detection, 'id' | 'bbox' | 'state' | 'top_matches' | 'assigned_sku' | 'det_confidence'>[];
  estimates: LiveEstimate[];
}
export interface SummaryRow {
  sku: string; name: string; detected: number; reviewed: number; expected: number; variance: number;
  status: 'SHORTFALL' | 'OK' | 'SURPLUS'; within_tolerance: boolean;
}
export interface Totals { detected: number; sure: number; check: number; unknown: number; unresolved: number }
export interface Permissions { can_edit: boolean; can_submit: boolean; can_approve: boolean; can_reopen: boolean; [k: string]: boolean }
export interface TimelineEntry { at: string; actor: string; event: string; note?: string }
export interface ScanView {
  scan_id: string; status: ScanStatus; store_id: string; shelf_id: string;
  created_by: UserRef; submitted_by: UserRef | null;
  approval: { decided_by: UserRef; decision: 'APPROVED' | 'RETURNED'; note: string | null; at: string } | null;
  image: { url: string; display_url: string; width: number; height: number };
  detections: Detection[]; summary: SummaryRow[]; totals: Totals;
  permissions: Permissions; timeline: TimelineEntry[]; pinned_detection_ids: string[];
}
export interface ScanItem {
  scan_id: string; status: ScanStatus; shelf_id: string; shelf_name: string;
  created_by: UserRef; submitted_by: UserRef | null; created_at: string; totals: Totals; device: string | null;
  submitted_at?: string; submit_note?: string | null; can_approve?: boolean;
}
export interface Progress { scan_id: string; status: ScanStatus; step: string | null; pct: number | null; found: number | null; failure_reason: string | null }

/** Server-side index state of a product: ready = photos AND the matching service confirmed them; pending = no photos yet;
 *  failed = photos stored but the matching service never confirmed them (retry with reindex). */
export type ProductStatus = 'ready' | 'pending' | 'failed';
export type ReferenceSource = 'upload' | 'import' | 'review_crop';
export interface Product {
  sku: string; name: string; min_qty: number; active: boolean; reference_count: number;
  barcode: string | null; brand: string | null; source: 'manual' | 'openfoodfacts'; source_url: string | null;
  indexed_at: string | null; status: ProductStatus; image_license: string | null; image_attribution: string | null;
  references: { id: string; source: ReferenceSource; thumb_url: string; url: string }[];
}
export interface ProductQuery { q?: string; limit?: number; offset?: number; slim?: boolean; status?: ProductStatus }
export interface RecognitionPreview {
  sku: string; name: string; scan_created: false; close_match_margin: number;
  detections: { bbox: [number, number, number, number]; detector_score: number; target_score: number | null;
    close_match: boolean; match_probability: number | null; matches: { sku: string; name: string; score: number }[] }[];
}
export interface IndexStatus {
  products: Record<ProductStatus, number>; reachable: boolean; model: string | null; collection: string | null;
  worker_references: number | null; stale: number; warnings: string[];
}
export interface Batch {
  id: string; source: string; started_at: string; finished_at: string | null; running: boolean;
  counts: Record<string, unknown> & { warnings?: string[]; signature?: string | null };
  params: Record<string, unknown>; errors: { key: string; kind: string; error: string }[];
}
export interface CreditItem { sku: string; name: string; brand: string | null; license: string; attribution: string | null; source_url: string | null }
export interface CreditPage { total: number; items: CreditItem[] }
export interface Shelf { id: string; name: string; store_id: string; store_name?: string }
export interface Store { id: string; name: string }
export interface ExpectedRow { sku: string; shelf_id?: string; expected_qty: number; [k: string]: unknown }
export interface AppUser { id: string; name: string; email: string; role: Role; active: boolean; pending?: boolean; last_active?: string | null }
export interface Notification { id: string; kind: string; text: string; scan_id: string | null; read: boolean; at: string }
export type PatchAction = 'ASSIGN' | 'NOT_A_PRODUCT' | 'UNKNOWN' | 'CONFIRM' | 'RESET';
