import { useEffect, useRef, useState } from 'react';
import { useIntro } from '../../lib/intro';
import { cn } from '../../utils/cn';

/**
 * After the video ends:
 *  false -> the video fades away and the normal ShelfSense hero (paper + grid) is shown, as in the design mock-up.
 *  true  -> the video stays behind the UI, frozen on its last frame, under a readability wash.
 */
const KEEP_VIDEO_BEHIND_UI = false;

const BASE = import.meta.env.BASE_URL;
const SRC = {
  desktop: `${BASE}video/hero.mp4`,
  mobile: `${BASE}video/hero-mobile.mp4`,
  start: `${BASE}images/hero-poster.jpg`, // first frame: shown until the video starts
  end: `${BASE}images/hero-end.jpg`, // last frame: used when the video is not played
};
/** Small file on phones, full-size otherwise. Chosen once, in code, so no <source> error events are involved. */
const pickVideoSrc = () => (window.matchMedia('(max-width: 767px)').matches ? SRC.mobile : SRC.desktop);

/** If the video has not started playing within this time (slow network, blocked, broken), skip the intro. */
const START_TIMEOUT_MS = 6000;

/**
 * Full-bleed hero background video that doubles as the intro.
 * Plays once, muted. onEnded -> reveal the UI (the video holds its last frame as the background).
 * Fallbacks (the UI is never left hidden): autoplay blocked, load error, no start within 6s,
 * "Skip intro" button, and the global ceiling in IntroProvider.
 */
export function HeroVideo() {
  const { done, finish } = useIntro();
  const videoRef = useRef<HTMLVideoElement>(null);
  const startedDone = useRef(done); // intro was already over when we mounted: show the still end frame only
  const hasStarted = useRef(false);
  const [failed, setFailed] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [videoSrc] = useState(pickVideoSrc);
  const stillOnly = startedDone.current || failed;

  useEffect(() => {
    const video = videoRef.current;
    if (stillOnly || !video) return;
    void video.play().catch(() => {
      // autoplay blocked by the browser: don't make the visitor wait on a video that can't play
      setFailed(true);
      finish();
    });
    const t = window.setTimeout(() => {
      if (!hasStarted.current) {
        setFailed(true);
        finish();
      }
    }, START_TIMEOUT_MS);
    return () => window.clearTimeout(t);
  }, [stillOnly, finish]);

  const fail = () => {
    setFailed(true);
    finish();
  };

  const skip = () => {
    const video = videoRef.current;
    if (video && Number.isFinite(video.duration)) {
      video.currentTime = video.duration; // jump to the last frame so the background matches the end state
      video.pause();
    }
    finish();
  };

  return (
    <div
      aria-hidden
      className={cn(
        'absolute inset-0 z-0 overflow-hidden bg-cream transition-opacity duration-[1200ms] ease-out',
        done && !KEEP_VIDEO_BEHIND_UI && 'pointer-events-none opacity-0',
      )}
    >
      <img src={stillOnly ? SRC.end : SRC.start} alt="" decoding="async" fetchPriority="high" className="absolute inset-0 h-full w-full object-cover" />
      {!stillOnly && (
        <video
          ref={videoRef}
          src={videoSrc}
          autoPlay
          muted
          playsInline
          preload="auto"
          poster={SRC.start}
          tabIndex={-1}
          onPlaying={() => {
            hasStarted.current = true;
            setPlaying(true);
          }}
          onEnded={finish}
          onError={fail}
          className={cn('absolute inset-0 h-full w-full object-cover transition-opacity duration-500', playing ? 'opacity-100' : 'opacity-0')}
        />
      )}

      {/* legibility scrims (only when the video stays behind the UI): hidden during the intro, fade in with the UI */}
      {KEEP_VIDEO_BEHIND_UI && (
        <>
          <div className="intro-reveal absolute inset-0 bg-paper/80 lg:hidden" />
          <div
            className="intro-reveal absolute inset-0 hidden lg:block"
            style={{ backgroundImage: 'linear-gradient(to right, rgba(246,247,244,0.96) 0%, rgba(246,247,244,0.9) 34%, rgba(246,247,244,0.5) 52%, rgba(246,247,244,0) 76%)' }}
          />
          <div className="intro-reveal absolute inset-x-0 bottom-0 h-24 bg-gradient-to-t from-paper to-transparent" />
        </>
      )}

      {!done && (
        <button
          type="button"
          onClick={skip}
          aria-hidden={false}
          className="fixed bottom-6 right-6 z-[60] rounded-full bg-ink/85 px-4 py-2 text-sm font-semibold text-white shadow-card backdrop-blur transition-colors hover:bg-ink"
        >
          Skip intro
        </button>
      )}
    </div>
  );
}
