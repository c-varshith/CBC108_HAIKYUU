import { useEffect, useRef, useState } from 'react';

type Phase = 'idle' | 'cover' | 'reveal';
const COVER_MS = 620;
const REVEAL_MS = 520;

/**
 * Landing -> dashboard transition. Any link to "#/app..." clicked on the landing page first plays a
 * circular green wipe with a scan line (expanding from the click point), switches the hash while the
 * screen is covered, then fades out to reveal the dashboard. Skipped for reduced-motion, modified
 * clicks (new tab) and navigation that is already inside the dashboard.
 */
export function RouteTransition() {
  const [phase, setPhase] = useState<Phase>('idle');
  const [origin, setOrigin] = useState({ x: 50, y: 50 });
  const busy = useRef(false);
  const timers = useRef<number[]>([]);

  useEffect(() => {
    const later = (fn: () => void, ms: number) => timers.current.push(window.setTimeout(fn, ms));

    const start = (href: string, x: number, y: number) => {
      if (busy.current) return;
      busy.current = true;
      setOrigin({ x, y });
      setPhase('cover');
      later(() => {
        window.location.hash = href;
        later(() => setPhase('reveal'), 80);
        later(() => {
          setPhase('idle');
          busy.current = false;
        }, 80 + REVEAL_MS);
      }, COVER_MS);
    };

    const onClick = (e: MouseEvent) => {
      if (busy.current || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const link = (e.target as Element | null)?.closest?.('a[href^="#/app"]') as HTMLAnchorElement | null;
      if (!link || link.target === '_blank') return;
      if (window.location.hash.startsWith('#/app')) return;
      if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

      e.preventDefault();
      start(link.getAttribute('href') ?? '#/app', (e.clientX / window.innerWidth) * 100, (e.clientY / window.innerHeight) * 100);
    };

    document.addEventListener('click', onClick);
    return () => {
      document.removeEventListener('click', onClick);
      timers.current.forEach(window.clearTimeout);
      timers.current = [];
    };
  }, []);

  const circle = (r: string) => `circle(${r} at ${origin.x}% ${origin.y}%)`;
  const overlay: React.CSSProperties =
    phase === 'idle'
      ? { clipPath: circle('0%'), opacity: 1, visibility: 'hidden', transition: 'none', pointerEvents: 'none' }
      : phase === 'cover'
        ? { clipPath: circle('150%'), opacity: 1, visibility: 'visible', transition: `clip-path ${COVER_MS}ms cubic-bezier(0.7, 0, 0.2, 1)`, pointerEvents: 'auto' }
        : { clipPath: circle('150%'), opacity: 0, visibility: 'visible', transition: `opacity ${REVEAL_MS}ms ease`, pointerEvents: 'none' };
  const line: React.CSSProperties =
    phase === 'idle' ? { transform: 'translateY(-2vh)', transition: 'none' } : { transform: 'translateY(102vh)', transition: `transform ${COVER_MS + 200}ms linear` };

  return (
    <div aria-hidden className="fixed inset-0 z-[100] bg-brand-deep" style={overlay}>
      <div className="absolute inset-x-0 top-0 h-px bg-white/80 shadow-[0_0_28px_8px_rgba(22,162,94,0.85)]" style={line} />
    </div>
  );
}
