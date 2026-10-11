import { useEffect, useRef, useState } from 'react';
import { useIntro } from '../../lib/intro';
import { cn } from '../../utils/cn';

const BASE = import.meta.env.BASE_URL;
const SRC = {
  desktop: `${BASE}video/hero.mp4`,
  mobile: `${BASE}video/hero-mobile.mp4`,
  end: `${BASE}images/hero-end.jpg`, // last frame of the intro: the panel starts exactly where the intro stopped
};
/** The panel loops the scan scene only (machine + eye), from this second to the end of the file. */
const LOOP_START = 2.6;
const FADE_BEFORE_END = 0.6;

const pickSrc = () => (window.matchMedia('(max-width: 767px)').matches ? SRC.mobile : SRC.desktop);

/**
 * The hero video panel (right side on desktop, under the copy on mobile). It appears with the page after the
 * intro, holds the intro's last frame, then loops the scan scene with a soft dissolve at the seam.
 * Reduced-motion visitors keep the still frame. Pauses when off-screen.
 */
export function HeroPanel() {
  const { done } = useIntro();
  const boxRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [src] = useState(pickSrc);
  const [enabled, setEnabled] = useState(false);
  const [live, setLive] = useState(false);

  useEffect(() => {
    if (!done || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const t = window.setTimeout(() => setEnabled(true), 2200);
    return () => window.clearTimeout(t);
  }, [done]);

  useEffect(() => {
    const box = boxRef.current;
    if (!enabled || !box) return;
    const io = new IntersectionObserver(([e]) => {
      const v = videoRef.current;
      if (!v) return;
      if (e.isIntersecting) void v.play().catch(() => {});
      else v.pause();
    });
    io.observe(box);
    return () => io.disconnect();
  }, [enabled]);

  const restart = () => {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = LOOP_START;
    void v.play().catch(() => {});
  };

  return (
    <div
      ref={boxRef}
      aria-hidden
      style={{ '--d': '150ms' } as React.CSSProperties}
      className="intro-reveal hero-video-fade relative mx-4 mb-12 mt-10 aspect-video overflow-hidden rounded-2xl bg-cream sm:mx-6 lg:absolute lg:inset-y-auto lg:left-[42%] lg:right-0 lg:top-1/2 lg:z-0 lg:m-0 lg:-translate-y-1/2 lg:rounded-none lg:bg-transparent"
    >
      <img src={SRC.end} alt="" decoding="async" className="absolute inset-0 h-full w-full object-cover" />
      {enabled && (
        <video
          ref={videoRef}
          src={src}
          muted
          playsInline
          preload="auto"
          tabIndex={-1}
          onLoadedMetadata={restart}
          onPlaying={() => setLive(true)}
          onTimeUpdate={(e) => {
            const v = e.currentTarget;
            if (v.duration - v.currentTime < FADE_BEFORE_END) setLive(false); // dissolve back to the last frame before the seam
          }}
          onEnded={restart}
          className={cn('absolute inset-0 h-full w-full object-cover transition-opacity duration-700', live ? 'opacity-100' : 'opacity-0')}
        />
      )}
    </div>
  );
}
