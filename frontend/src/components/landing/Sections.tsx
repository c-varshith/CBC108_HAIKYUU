import { ArrowRight, ClipboardCheck, PackageSearch, Tags } from 'lucide-react';
import { Logo, Reveal, SectionHeading } from '../ui';

const FEATURES = [
  {
    icon: PackageSearch,
    title: 'Detect visible packages',
    text: 'The trained detector locates packages in shelf photos and estimates quantities from the visible boxes.',
  },
  {
    icon: Tags,
    title: 'Match your catalog',
    text: 'Product reference photos help match detections to SKUs. Unmatched items remain Unknown for staff review.',
  },
  {
    icon: ClipboardCheck,
    title: 'Review before approval',
    text: 'Staff correct uncertain counts and submit the scan. A manager reviews and approves the final count.',
  },
];

export function Features() {
  return (
    <section id="features" className="mx-auto max-w-7xl scroll-mt-20 px-4 py-16 sm:px-6 lg:px-8 lg:py-20">
      <SectionHeading
        eyebrow="Capabilities"
        title="From shelf photo to reviewed count"
        copy="Detection, catalog matching and human approval in one inventory workflow."
      />
      <div className="mt-10 grid gap-4 md:grid-cols-3">
        {FEATURES.map((feature, index) => (
          <Reveal key={feature.title} delay={index * 70}>
            <article className="h-full rounded-2xl border border-line bg-white p-6 shadow-card">
              <span className="grid h-11 w-11 place-items-center rounded-xl bg-brand-mist text-brand-deep">
                <feature.icon className="h-5 w-5" aria-hidden="true" />
              </span>
              <h3 className="mt-4 text-base font-bold text-ink">{feature.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-muted">{feature.text}</p>
            </article>
          </Reveal>
        ))}
      </div>
    </section>
  );
}

const STEPS = [
  { title: 'Capture', text: 'Choose a shelf and upload a photo or capture one with your camera.' },
  { title: 'Review', text: 'Check detected packages, correct SKU matches and adjust quantities.' },
  { title: 'Approve', text: 'Submit the reviewed count for manager approval and stock comparison.' },
];

export function HowItWorks() {
  return (
    <section id="how-it-works" className="scroll-mt-20 border-y border-line bg-white">
      <div className="mx-auto max-w-7xl px-4 py-16 sm:px-6 lg:px-8 lg:py-20">
        <SectionHeading eyebrow="Workflow" title="A simple, reviewable process" />
        <ol className="mx-auto mt-9 grid max-w-5xl gap-4 md:grid-cols-3">
          {STEPS.map((step, index) => (
            <Reveal as="li" key={step.title} delay={index * 70} className="rounded-2xl border border-line bg-paper p-5">
              <div className="flex items-center gap-3">
                <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-ink text-sm font-bold text-white">{index + 1}</span>
                <h3 className="font-bold text-ink">{step.title}</h3>
              </div>
              <p className="mt-3 text-sm leading-relaxed text-muted">{step.text}</p>
            </Reveal>
          ))}
        </ol>
      </div>
    </section>
  );
}

export function Footer() {
  return (
    <footer className="border-t border-line bg-white">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-5 px-4 py-6 sm:px-6 lg:px-8">
        <Logo />
        <nav aria-label="Footer" className="flex items-center gap-5 text-sm text-muted">
          <a href="#features" className="transition-colors hover:text-ink">Features</a>
          <a href="#how-it-works" className="transition-colors hover:text-ink">Workflow</a>
          <a href="#/app" className="inline-flex items-center gap-1 font-semibold text-ink transition-colors hover:text-brand-deep">
            Sign in <ArrowRight className="h-3.5 w-3.5" />
          </a>
        </nav>
        <p className="w-full text-xs text-muted sm:w-auto">© {new Date().getFullYear()} Calibrex</p>
      </div>
    </footer>
  );
}
