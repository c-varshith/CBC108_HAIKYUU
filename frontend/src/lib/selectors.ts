import type { Detection, ScanStatus } from './contract';

export function classify(matches: { sku: string; score: number }[], thresholds: { sure: number; margin: number; check: number }) {
  if (!matches.length || matches[0].score < thresholds.check) return 'UNKNOWN' as const;
  const top = matches[0];
  const other = matches.find((item) => item.sku !== top.sku);
  const margin = Math.round((top.score - (other?.score ?? 0)) * 1_000_000) / 1_000_000;
  return top.score >= thresholds.sure && margin >= thresholds.margin ? 'SURE' as const : 'CHECK' as const;
}

export function detectionCounts(detections: Detection[]) {
  const live = detections.filter((d) => !d.deleted);
  return {
    ALL: live.length,
    SURE: live.filter((d) => d.state === 'SURE').length,
    CHECK: live.filter((d) => d.state === 'CHECK' && !d.accepted_unknown).length,
    UNKNOWN: live.filter((d) => d.state === 'UNKNOWN' && !d.accepted_unknown).length,
    CORRECTED: live.filter((d) => d.state === 'CORRECTED').length,
    unresolved: live.filter((d) => !d.accepted_unknown && (d.state === 'CHECK' || d.state === 'UNKNOWN')).length,
  };
}

const TRANSITIONS: Record<ScanStatus, ScanStatus[]> = {
  DRAFT: ['UPLOADED', 'ARCHIVED'], UPLOADED: ['PROCESSING', 'FAILED'],
  PROCESSING: ['NEEDS_REVIEW', 'AUTO_READY', 'FAILED'], NEEDS_REVIEW: ['IN_REVIEW', 'SUBMITTED'],
  IN_REVIEW: ['SUBMITTED'], AUTO_READY: ['SUBMITTED', 'IN_REVIEW'], SUBMITTED: ['APPROVED', 'RETURNED'],
  RETURNED: ['IN_REVIEW', 'SUBMITTED'], APPROVED: ['REOPENED', 'ARCHIVED'], REOPENED: ['IN_REVIEW', 'SUBMITTED'],
  FAILED: ['UPLOADED'], ARCHIVED: [],
};
export function canTransition(from: ScanStatus, to: ScanStatus): boolean { return TRANSITIONS[from].includes(to); }

export function scaleBox(box: [number, number, number, number], source: { width: number; height: number }, target: { width: number; height: number }) {
  const sx = target.width / source.width, sy = target.height / source.height;
  return [box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy] as [number, number, number, number];
}

export type UndoSnapshot = { id: string; state: Detection['state']; sku: string | null; deleted?: boolean; accepted?: boolean };
export function groupUndo(snapshot: UndoSnapshot[]) {
  const grouped = new Map<string, { action: 'RESET' | 'NOT_A_PRODUCT' | 'UNKNOWN' | 'ASSIGN'; sku?: string; ids: string[] }>();
  for (const p of snapshot) {
    const untouched = p.state !== 'CORRECTED';
    const action = untouched ? 'RESET' : p.deleted ? 'NOT_A_PRODUCT' : p.accepted ? 'UNKNOWN' : p.sku ? 'ASSIGN' : 'RESET';
    const key = `${action}:${action === 'ASSIGN' ? p.sku : ''}`;
    const row = grouped.get(key) ?? { action, ...(action === 'ASSIGN' ? { sku: p.sku! } : {}), ids: [] };
    row.ids.push(p.id); grouped.set(key, row);
  }
  return [...grouped.values()];
}
