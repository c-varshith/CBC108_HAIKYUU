import { useEffect, useState } from 'react';
import { Navbar } from './components/Navbar';
import { Hero } from './components/landing/Hero';
import { Features, Footer, HowItWorks } from './components/landing/Sections';
import { AuthProvider } from './lib/auth';
import { Workspace } from './components/app/Workspace';
import { ResetPasswordPage } from './components/app/Login';
import { IntroProvider } from './lib/intro';
import { RouteTransition } from './components/RouteTransition';

function useHashRoute(): string {
  const [hash, setHash] = useState(() => window.location.hash);
  useEffect(() => {
    const onChange = () => {
      const prev = window.location.hash;
      setHash(prev);
      // Jump to the top when moving between the site and the workspace,
      // or between workspace pages. Landing anchors keep smooth scroll.
      const toApp = prev.startsWith('#/app') || prev.startsWith('#/invite') || prev.startsWith('#/reset-password');
      if (toApp) window.scrollTo({ top: 0 });
    };
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);
  return hash;
}

function Landing() {
  // When arriving from the workspace with a section hash, scroll to it
  // after the landing tree has painted (the browser can't anchor-scroll
  // into a tree that didn't exist at hashchange time).
  useEffect(() => {
    const hash = window.location.hash;
    if (hash && !hash.startsWith('#/') && hash.length > 1) {
      const t = window.setTimeout(() => {
        document.querySelector(hash)?.scrollIntoView({ behavior: 'smooth' });
      }, 60);
      return () => window.clearTimeout(t);
    }
  }, []);

  return (
    <IntroProvider>
      <Navbar />
      <main>
        <Hero />
        <div className="intro-reveal" style={{ '--d': '300ms' } as React.CSSProperties}>
          <Features />
          <HowItWorks />
        </div>
      </main>
      <div className="intro-reveal" style={{ '--d': '300ms' } as React.CSSProperties}>
        <Footer />
      </div>
      <RouteTransition />
    </IntroProvider>
  );
}

export default function App() {
  const hash = useHashRoute();
  const reset = hash.match(/^#\/reset-password\/(.+)$/);
  if (reset) return <ResetPasswordPage token={decodeURIComponent(reset[1])} />;
  if (hash.startsWith('#/app') || hash.startsWith('#/invite')) return <AuthProvider><Workspace hash={hash} /></AuthProvider>;
  return <Landing />;
}
