import { describe, expect, it } from 'vitest';
import { describeCameraError } from './camera';

describe('describeCameraError', () => {
  const kind = (name: string) => describeCameraError(new DOMException('x', name)).kind;
  it('maps browser errors to actionable kinds', () => {
    expect(kind('NotAllowedError')).toBe('denied');
    expect(kind('NotFoundError')).toBe('no-camera');
    expect(kind('NotReadableError')).toBe('busy');
    expect(describeCameraError(new Error('boom')).kind).toBe('other');
  });
  it('tells the user what to do when permission is blocked', () => {
    expect(describeCameraError(new DOMException('x', 'NotAllowedError')).message).toMatch(/browser settings/);
  });
});
