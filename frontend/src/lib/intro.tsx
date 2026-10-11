import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';

/**
 * Intro sequence state. While `done` is false the landing UI is hidden (see .intro-reveal in index.css),
 * page scrolling is locked, and only the hero video is visible. `finish()` reveals the UI.
 *
 * The intro ALWAYS plays on load. The only time it is skipped is when someone arrives on a section link
 * such as "#features" (they asked for a specific section, so we don't block it).
 * The UI is never left hidden: autoplay blocked, load error, no start in time, "Skip intro", or the
 * INTRO_MAX_MS ceiling all call finish().
 */
export const INTRO_MAX_MS = 15000;

type IntroState = { done: boolean; finish: () => void };
const IntroContext = createContext<IntroState>({ done: true, finish: () => {} });
export const useIntro = () => useContext(IntroContext);

const arrivedOnSectionLink = () => {
  const h = window.location.hash;
  return h.length > 1 && !h.startsWith('#/');
};

export function IntroProvider({ children }: { children: ReactNode }) {
  const [done, setDone] = useState(arrivedOnSectionLink);

  const finish = useCallback(() => {
    setDone(true);
  }, []);

  useEffect(() => {
    if (done) return;
    const t = window.setTimeout(finish, INTRO_MAX_MS);
    const root = document.documentElement;
    root.classList.add('intro-lock');
    return () => {
      window.clearTimeout(t);
      root.classList.remove('intro-lock');
    };
  }, [done, finish]);

  const value = useMemo(() => ({ done, finish }), [done, finish]);
  return (
    <IntroContext.Provider value={value}>
      <div data-intro={done ? 'done' : 'playing'}>{children}</div>
    </IntroContext.Provider>
  );
}
