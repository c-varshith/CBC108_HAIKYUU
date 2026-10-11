import { ArrowRight, Fingerprint, MonitorCheck, Plug } from 'lucide-react';
import { Eyebrow, Reveal, btnPrimary } from '../ui';
import { HeroPanel } from './HeroPanel';
import { HeroVideo } from './HeroVideo';

const MICRO = [
  { icon: Fingerprint, label: 'Catalog SKU matching' },
  { icon: MonitorCheck, label: 'Visible package counts' },
  { icon: Plug, label: 'Staff review and approval' },
];

export function Hero() {
  return (
    <section id="top" className="relative -mt-16 flex min-h-svh flex-col justify-center overflow-hidden pt-16">
      {/* intro: full-screen video. The whole UI stays hidden until it ends, then the video panel below takes over. */}
      <HeroVideo />

      {/* backdrop: hairline grid fading into paper */}
      <div
        aria-hidden
        className="intro-reveal pointer-events-none absolute inset-0"
        style={{
          backgroundImage:
            'linear-gradient(to right, rgba(20,26,22,0.045) 1px, transparent 1px), linear-gradient(to bottom, rgba(20,26,22,0.045) 1px, transparent 1px)',
          backgroundSize: '56px 56px',
          maskImage: 'radial-gradient(ellipse 90% 70% at 50% 0%, black 30%, transparent 78%)',
          WebkitMaskImage: 'radial-gradient(ellipse 90% 70% at 50% 0%, black 30%, transparent 78%)',
        }}
      />

      <div style={{ '--d': '150ms' } as React.CSSProperties} className="intro-reveal relative z-10 mx-auto grid w-full max-w-7xl items-center gap-14 px-4 pb-20 pt-14 sm:px-6 sm:pt-20 lg:grid-cols-12 lg:gap-10 lg:pb-28 lg:pt-24 lg:px-8">
        {/* Copy */}
        <div className="lg:col-span-5">
          <Reveal>
            <Eyebrow>
              <span className="h-1.5 w-1.5 animate-blink rounded-full bg-brand-bright" />
              Image-based inventory intelligence
            </Eyebrow>
          </Reveal>

          <Reveal delay={90}>
            <h1 className="mt-6 text-balance text-[2.75rem] font-extrabold leading-[1.04] tracking-[-0.04em] text-ink sm:text-6xl lg:text-[3.25rem] xl:text-6xl">
              Know what&rsquo;s on{' '}
              <span className="relative inline-block text-brand-deep">
                every shelf.
                <svg aria-hidden viewBox="0 0 220 12" className="absolute -bottom-1 left-0 w-full text-brand/50" preserveAspectRatio="none">
                  <path d="M3 9C60 3 160 3 217 8" stroke="currentColor" strokeWidth="5" strokeLinecap="round" fill="none" />
                </svg>
              </span>
            </h1>
          </Reveal>

          <Reveal delay={180}>
            <p className="mt-6 max-w-xl text-pretty text-lg leading-relaxed text-muted">
              Turn shelf photos into estimated product counts. Match detections to your catalog, correct uncertain items, and submit
              reviewed counts for approval.
            </p>
          </Reveal>

          <Reveal delay={260}>
            <div className="mt-8 flex flex-wrap items-center gap-3">
              <a href="#/app/scan" className={btnPrimary}>
                Open workspace
                <ArrowRight className="h-4 w-4" />
              </a>
            </div>
          </Reveal>

          <Reveal delay={340}>
            <ul className="mt-10 flex flex-wrap gap-x-6 gap-y-2">
              {MICRO.map((m) => (
                <li key={m.label} className="flex items-center gap-2 font-mono text-[11px] font-medium text-muted">
                  <m.icon className="h-3.5 w-3.5 text-brand" />
                  {m.label}
                </li>
              ))}
            </ul>
          </Reveal>
        </div>

      </div>

      {/* the video, in the spot the detection preview used to occupy */}
      <HeroPanel />
    </section>
  );
}
