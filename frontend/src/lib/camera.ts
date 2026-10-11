import { useCallback, useEffect, useRef, useState } from 'react';

export type CameraState =
  | { status: 'idle' }
  | { status: 'requesting' }
  | { status: 'active'; width: number; height: number; label: string }
  | { status: 'error'; kind: 'denied' | 'no-camera' | 'busy' | 'insecure' | 'unsupported' | 'other'; message: string };

export type Facing = 'environment' | 'user';

/** Turn a getUserMedia failure into something a store employee can act on. */
export function describeCameraError(err: unknown): Extract<CameraState, { status: 'error' }> {
  const name = err instanceof DOMException ? err.name : '';
  const e = (kind: Extract<CameraState, { status: 'error' }>['kind'], message: string) => ({ status: 'error' as const, kind, message });
  if (name === 'NotAllowedError' || name === 'SecurityError')
    return e('denied', 'Camera access was blocked. Allow the camera for this site in your browser settings, then try again.');
  if (name === 'NotFoundError' || name === 'OverconstrainedError') return e('no-camera', 'No camera was found on this device.');
  if (name === 'NotReadableError' || name === 'AbortError') return e('busy', 'The camera is in use by another app. Close it and try again.');
  return e('other', 'The camera could not be started.');
}

/**
 * Camera lifecycle: permission prompt, start/stop/switch, track-ended detection (unplugged / revoked) and cleanup.
 * `capture()` grabs the current frame as a JPEG scaled so its long side is <= maxSide (keeps live uploads small).
 */
export function useCamera() {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const [state, setState] = useState<CameraState>({ status: 'idle' });
  const [facing, setFacing] = useState<Facing>('environment');

  const stop = useCallback(() => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
    setState({ status: 'idle' });
  }, []);

  const start = useCallback(
    async (want: Facing = facing) => {
      if (!window.isSecureContext) {
        setState({ status: 'error', kind: 'insecure', message: 'Camera access needs HTTPS (or localhost). Open the app over a secure address.' });
        return false;
      }
      if (!navigator.mediaDevices?.getUserMedia) {
        setState({ status: 'error', kind: 'unsupported', message: 'This browser does not support camera access.' });
        return false;
      }
      streamRef.current?.getTracks().forEach((t) => t.stop());
      setState({ status: 'requesting' });
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: want }, width: { ideal: 1920 }, height: { ideal: 1080 } },
          audio: false,
        });
        streamRef.current = stream;
        const track = stream.getVideoTracks()[0];
        track.addEventListener('ended', () => {
          if (streamRef.current === stream) {
            streamRef.current = null;
            setState({ status: 'error', kind: 'busy', message: 'The camera was disconnected or access was revoked.' });
          }
        });
        const v = videoRef.current;
        if (v) {
          v.srcObject = stream;
          v.muted = true;
          await v.play().catch(() => undefined);
        }
        const s = track.getSettings();
        setFacing(want);
        setState({ status: 'active', width: s.width ?? v?.videoWidth ?? 0, height: s.height ?? v?.videoHeight ?? 0, label: track.label || 'Camera' });
        return true;
      } catch (err) {
        setState(describeCameraError(err));
        return false;
      }
    },
    [facing],
  );

  const flip = useCallback(() => start(facing === 'environment' ? 'user' : 'environment'), [facing, start]);

  const capture = useCallback(async (maxSide = 1280, quality = 0.8): Promise<{ blob: Blob; width: number; height: number } | null> => {
    const v = videoRef.current;
    if (!v || !v.videoWidth || v.readyState < 2) return null;
    const k = Math.min(1, maxSide / Math.max(v.videoWidth, v.videoHeight));
    const w = Math.round(v.videoWidth * k);
    const h = Math.round(v.videoHeight * k);
    const c = document.createElement('canvas');
    c.width = w;
    c.height = h;
    c.getContext('2d')?.drawImage(v, 0, 0, w, h);
    const blob = await new Promise<Blob | null>((r) => c.toBlob(r, 'image/jpeg', quality));
    return blob ? { blob, width: w, height: h } : null;
  }, []);

  useEffect(() => () => streamRef.current?.getTracks().forEach((t) => t.stop()), []);

  return { videoRef, state, facing, start, stop, flip, capture };
}
