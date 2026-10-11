import { ScanBarcode } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { cn } from '../utils/cn';

export const btnPrimary =
  'inline-flex items-center justify-center gap-2 rounded-full bg-brand px-5 py-2.5 text-sm font-semibold text-white shadow-[0_10px_24px_-10px_rgba(12,122,69,0.7)] transition-all duration-200 hover:bg-brand-deep hover:shadow-[0_14px_28px_-10px_rgba(12,122,69,0.8)] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-brand';

export const btnSecondary =
  'inline-flex items-center justify-center gap-2 rounded-full border border-line-strong bg-white px-5 py-2.5 text-sm font-semibold text-ink transition-all duration-200 hover:border-ink/30 hover:bg-cream active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50';

export const btnGhost =
  'inline-flex items-center justify-center gap-2 rounded-full px-4 py-2 text-sm font-semibold text-muted transition-colors duration-200 hover:bg-ink/[0.05] hover:text-ink';

export function Logo({ compact = false }: { compact?: boolean }) {
  return (
    <span className="inline-flex items-center gap-2.5">
      <span className="grid h-8 w-8 place-items-center rounded-lg bg-gradient-to-br from-brand-bright to-brand-deep text-white shadow-[0_6px_16px_-6px_rgba(12,122,69,0.8)]">
        <ScanBarcode className="h-[18px] w-[18px]" strokeWidth={2.2} />
      </span>
      {!compact && <span className="text-[17px] font-bold tracking-tight text-ink">Calibrex</span>}
    </span>
  );
}

export function Reveal({
  children,
  delay = 0,
  className,
  as: Tag = 'div',
}: {
  children: ReactNode;
  delay?: number;
  className?: string;
  as?: 'div' | 'section' | 'li' | 'span';
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [inView, setInView] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        setInView(true);
        io.disconnect();
      }
    }, { threshold: 0.12, rootMargin: '0px 0px -8% 0px' });
    io.observe(el);
    return () => io.disconnect();
  }, []);

  return (
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    <Tag ref={ref as any} className={cn('reveal', inView && 'in-view', className)} style={{ '--reveal-delay': `${delay}ms` } as React.CSSProperties}>
      {children}
    </Tag>
  );
}

export function Eyebrow({ children }: { children: ReactNode }) {
  return <span className="inline-flex items-center gap-2 rounded-full border border-brand/20 bg-brand-mist px-3 py-1 font-mono text-[11px] font-semibold uppercase tracking-[0.14em] text-brand-deep">{children}</span>;
}

export function SectionHeading({
  eyebrow,
  title,
  copy,
  className,
}: {
  eyebrow: ReactNode;
  title: ReactNode;
  copy?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('mx-auto max-w-2xl text-center', className)}>
      <Reveal><Eyebrow>{eyebrow}</Eyebrow></Reveal>
      <Reveal delay={80}><h2 className="mt-4 text-balance text-3xl font-bold tracking-[-0.03em] text-ink sm:text-[2.5rem] sm:leading-[1.1]">{title}</h2></Reveal>
      {copy && <Reveal delay={160}><p className="mt-4 text-pretty text-base leading-relaxed text-muted">{copy}</p></Reveal>}
    </div>
  );
}
