import { describe, expect, it } from 'vitest';
import { roleCan } from './auth';
import type { Detection } from './contract';
import { canTransition, classify, detectionCounts, groupUndo, scaleBox } from './selectors';

const det = (id: string, state: Detection['state'], extra: Partial<Detection> = {}): Detection => ({
  id, state, bbox: [10, 20, 30, 50], top_matches: [], assigned_sku: null, det_confidence: 0.9, corrected_by: null, ...extra,
});

describe('frontend selectors and workflow helpers', () => {
  it('scales absolute image boxes into display coordinates', () => {
    expect(scaleBox([10, 20, 30, 50], { width: 100, height: 100 }, { width: 200, height: 50 }))
      .toEqual([20, 10, 60, 25]);
  });

  it('rounds score margin to six decimals, matching backend classification', () => {
    expect(classify([{ sku: 'A', score: 0.85 }, { sku: 'B', score: 0.77 }], { sure: 0.8, margin: 0.08, check: 0.5 })).toBe('SURE');
    expect(classify([], { sure: 0.8, margin: 0.08, check: 0.5 })).toBe('UNKNOWN');
  });

  it('counts only live unresolved detections', () => {
    expect(detectionCounts([
      det('sure', 'SURE'), det('check', 'CHECK'), det('accepted', 'UNKNOWN', { accepted_unknown: true }),
      det('deleted', 'UNKNOWN', { deleted: true }), det('fix', 'CORRECTED'),
    ])).toEqual({ ALL: 4, SURE: 1, CHECK: 1, UNKNOWN: 0, CORRECTED: 1, unresolved: 1 });
  });

  it('mirrors role capabilities without replacing server enforcement', () => {
    expect(roleCan('OWNER', 'settings.thresholds')).toBe(true);
    expect(roleCan('AUDITOR', 'settings.thresholds')).toBe(false);
    expect(roleCan('AUDITOR', 'scan.view')).toBe(true);
    expect(roleCan('STAFF', 'scan.approve')).toBe(false);
  });

  it('allows only defined lifecycle transitions and preserves approved locking', () => {
    expect(canTransition('SUBMITTED', 'APPROVED')).toBe(true);
    expect(canTransition('APPROVED', 'REOPENED')).toBe(true);
    expect(canTransition('APPROVED', 'IN_REVIEW')).toBe(false);
    expect(canTransition('ARCHIVED', 'UPLOADED')).toBe(false);
  });

  it('groups an undo batch by the API action and previous SKU', () => {
    expect(groupUndo([
      { id: 'a', state: 'CORRECTED', sku: 'SKU-1' },
      { id: 'b', state: 'CORRECTED', sku: 'SKU-1' },
      { id: 'c', state: 'CORRECTED', sku: null, accepted: true },
      { id: 'd', state: 'CHECK', sku: 'SKU-2' },
    ])).toEqual([
      { action: 'ASSIGN', sku: 'SKU-1', ids: ['a', 'b'] },
      { action: 'UNKNOWN', ids: ['c'] },
      { action: 'RESET', ids: ['d'] },
    ]);
  });
});
