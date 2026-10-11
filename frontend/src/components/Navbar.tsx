import { ArrowRight, Menu, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { cn } from '../utils/cn';
import { Logo, btnPrimary } from './ui';

const LINKS = [
  { label: 'Features', href: '#features' },
  { label: 'Workflow', href: '#how-it-works' },
];

export function Navbar() {
  const [open, setOpen] = useState(false);
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, []);

  return (
    <header
      className={cn(
        'intro-reveal sticky inset-x-0 top-0 z-50 border-b transition-all duration-300',
        scrolled ? 'border-line bg-paper/85 shadow-[0_8px_32px_-20px_rgba(20,26,22,0.3)] backdrop-blur-xl' : 'border-transparent bg-paper/60 backdrop-blur-sm',
      )}
    >
      <nav aria-label="Primary" className="mx-auto flex h-16 max-w-7xl items-center justify-between gap-4 px-4 sm:px-6 lg:px-8">
        <a href="#top" className="shrink-0" aria-label="Calibrex home">
          <Logo />
        </a>

        <div className="hidden items-center gap-1 md:flex">
          {LINKS.map((l) => (
            <a
              key={l.href}
              href={l.href}
              className="rounded-full px-3.5 py-2 text-sm font-medium text-muted transition-colors hover:bg-ink/[0.05] hover:text-ink"
            >
              {l.label}
            </a>
          ))}
        </div>

        <div className="hidden items-center gap-3 md:flex">
          <a href="#/app" className="rounded-full px-3.5 py-2 text-sm font-semibold text-ink-2 transition-colors hover:bg-ink/[0.05] hover:text-ink">
            Sign in
          </a>
        </div>

        <button
          type="button"
          className="grid h-10 w-10 place-items-center rounded-xl border border-line bg-white text-ink md:hidden"
          aria-expanded={open}
          aria-label={open ? 'Close menu' : 'Open menu'}
          onClick={() => setOpen((v) => !v)}
        >
          {open ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
        </button>
      </nav>

      {/* Mobile menu */}
      <div
        className={cn(
          'grid overflow-hidden border-line transition-all duration-300 md:hidden',
          open ? 'grid-rows-[1fr] border-t border-line bg-paper' : 'grid-rows-[0fr]',
        )}
      >
        <div className="min-h-0">
          <div className="flex flex-col gap-1 px-4 py-4">
            {LINKS.map((l) => (
              <a
                key={l.href}
                href={l.href}
                onClick={() => setOpen(false)}
                className="rounded-xl px-4 py-3 text-sm font-medium text-ink-2 transition-colors hover:bg-cream"
              >
                {l.label}
              </a>
            ))}
            <a href="#/app" className={cn(btnPrimary, 'mt-2 w-full')} onClick={() => setOpen(false)}>
              Sign in
              <ArrowRight className="h-4 w-4" />
            </a>
          </div>
        </div>
      </div>
    </header>
  );
}
