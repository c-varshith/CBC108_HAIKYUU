import { Bot, Maximize2, MessageCircle, Minimize2, Send, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { inventoryAssistant } from '../../lib/api';
import { cn } from '../../utils/cn';

type Turn = { role: 'user' | 'assistant'; content: string };
const suggestions = ['How many active products are in my catalog?', 'Which products are low on stock?', 'How complete are our inventory counts?'];

function inlineText(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*)/g).filter(Boolean).map((part, index) => part.startsWith('**') && part.endsWith('**')
    ? <strong key={index} className="font-semibold">{part.slice(2, -2)}</strong>
    : <span key={index}>{part}</span>);
}

function AssistantText({ content }: { content: string }) {
  const lines = content.split(/\r?\n/);
  const blocks: ReactNode[] = [];
  for (let i = 0; i < lines.length;) {
    const line = lines[i].trim();
    if (!line) { i += 1; continue; }
    if (/^\|.*\|$/.test(line)) {
      const tableLines: string[] = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) tableLines.push(lines[i++].trim());
      const rows = tableLines.map((row) => row.slice(1, -1).split('|').map((cell) => cell.trim())).filter((row) => !row.every((cell) => /^:?-{3,}:?$/.test(cell)));
      const [head, ...body] = rows;
      blocks.push(<div key={`table-${i}`} className="my-2 overflow-x-auto rounded-lg border border-line"><table className="w-full border-collapse text-left text-xs"><thead className="bg-cream"><tr>{head?.map((cell, index) => <th key={index} className="px-2.5 py-2 font-semibold">{inlineText(cell)}</th>)}</tr></thead><tbody>{body.map((row, rowIndex) => <tr key={rowIndex} className="border-t border-line">{row.map((cell, cellIndex) => <td key={cellIndex} className="px-2.5 py-2">{inlineText(cell)}</td>)}</tr>)}</tbody></table></div>);
      continue;
    }
    if (/^[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*]\s+/, ''));
      blocks.push(<ul key={`list-${i}`} className="my-2 list-disc space-y-1 pl-5">{items.map((item, index) => <li key={index}>{inlineText(item)}</li>)}</ul>);
      continue;
    }
    if (/^\d+[.)]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*\d+[.)]\s+/, ''));
      blocks.push(<ol key={`ordered-${i}`} className="my-2 list-decimal space-y-1 pl-5">{items.map((item, index) => <li key={index}>{inlineText(item)}</li>)}</ol>);
      continue;
    }
    const paragraph = [line]; i += 1;
    while (i < lines.length && lines[i].trim() && !/^\s*(\||[-*]\s+|\d+[.)]\s+)/.test(lines[i])) paragraph.push(lines[i++].trim());
    blocks.push(<p key={`paragraph-${i}`} className="my-1.5">{inlineText(paragraph.join(' '))}</p>);
  }
  return <div className="space-y-1">{blocks}</div>;
}

export function InventoryAssistant() {
  const [open, setOpen] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const busyRef = useRef(false);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [turns, busy, error]);

  async function ask(question: string) {
    const message = question.trim();
    if (!message || busyRef.current) return;
    busyRef.current = true;
    const previousTurns = turns;
    setTurns((current) => [...current, { role: 'user', content: message }]);
    setDraft(''); setError(''); setBusy(true);
    try {
      const { answer } = await inventoryAssistant.chat(message, previousTurns.slice(-10));
      setTurns((current) => [...current, { role: 'assistant', content: answer }]);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not reach the assistant. Please try again.');
    } finally {
      busyRef.current = false;
      setBusy(false);
      window.setTimeout(() => inputRef.current?.focus(), 0);
    }
  }

  return <>
    {!open && <button type="button" onClick={() => { setOpen(true); window.setTimeout(() => inputRef.current?.focus(), 0); }} aria-label="Open CaliBot" title="Ask CaliBot about inventory and catalog" className="fixed bottom-24 right-4 z-40 grid h-14 w-14 place-items-center rounded-full bg-brand text-white shadow-[0_12px_30px_-8px_rgba(12,122,69,0.75)] transition hover:bg-brand-deep md:bottom-6 md:right-6">
      <MessageCircle className="h-6 w-6" />
    </button>}

    {fullscreen && <button type="button" aria-label="Close fullscreen backdrop" onClick={() => setFullscreen(false)} className="fixed inset-0 z-[59] bg-slate-950/30 backdrop-blur-[2px]" />}
    {open && <section aria-label="CaliBot" className={cn('fixed z-[60] flex flex-col overflow-hidden border border-line bg-white shadow-pop', fullscreen ? 'left-1/2 top-1/2 h-[calc(100dvh-1.5rem)] w-[calc(100vw-1.5rem)] -translate-x-1/2 -translate-y-1/2 rounded-2xl sm:h-[min(760px,calc(100dvh-4rem))] sm:w-[min(1100px,calc(100vw-4rem))]' : 'inset-x-3 bottom-24 h-[min(620px,calc(100dvh-8rem))] rounded-2xl sm:inset-x-auto sm:bottom-24 sm:right-5 sm:h-[min(620px,calc(100dvh-8rem))] sm:w-[min(430px,calc(100vw-2.5rem))] md:bottom-6')}>
      <header className="flex shrink-0 items-center justify-between border-b border-line bg-white px-4 py-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-brand-mist text-brand"><Bot className="h-5 w-5" /></span>
          <div className="min-w-0"><h2 className="truncate text-sm font-bold">CaliBot</h2><p className="text-[11px] text-muted">Ask about this supermarket’s catalog and stock</p></div>
        </div>
        <div className="ml-2 flex shrink-0 items-center gap-1">
          <button type="button" onClick={() => setFullscreen((value) => !value)} aria-label={fullscreen ? 'Exit fullscreen' : 'Open fullscreen'} title={fullscreen ? 'Exit fullscreen' : 'Fullscreen'} className="grid h-9 w-9 place-items-center rounded-lg text-muted hover:bg-cream hover:text-ink">{fullscreen ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}</button>
          <button type="button" onClick={() => { setOpen(false); setFullscreen(false); }} aria-label="Minimize assistant" title="Minimize" className="grid h-9 w-9 place-items-center rounded-lg text-muted hover:bg-cream hover:text-ink"><X className="h-4 w-4" /></button>
        </div>
      </header>

      <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto bg-paper/60 p-4" aria-live="polite">
        {turns.length === 0 && <div className="space-y-3">
          <div className="max-w-[90%] rounded-2xl rounded-tl-sm border border-line bg-white p-3 text-sm leading-relaxed text-ink">Hi! I can answer questions using your workspace’s catalog and latest approved inventory counts.</div>
          <p className="pt-1 text-[11px] font-semibold text-muted">Try asking</p>
          {suggestions.map((suggestion) => <button key={suggestion} type="button" disabled={busy} onClick={() => void ask(suggestion)} className="block w-full rounded-xl border border-line bg-white px-3 py-2.5 text-left text-xs text-ink hover:border-brand/50 hover:bg-brand-mist disabled:opacity-50">{suggestion}</button>)}
        </div>}
        {turns.map((turn, index) => <div key={`${index}-${turn.role}`} className={cn('w-fit max-w-[90%] whitespace-pre-wrap rounded-2xl px-3 py-2.5 text-sm leading-relaxed', turn.role === 'user' ? 'ml-auto rounded-tr-sm bg-brand text-white' : 'rounded-tl-sm border border-line bg-white text-ink')}>{turn.role === 'assistant' ? <AssistantText content={turn.content} /> : turn.content}</div>)}
        {busy && <div className="max-w-[90%] rounded-2xl rounded-tl-sm border border-line bg-white px-3 py-2.5 text-sm text-muted" role="status">Checking your workspace data…</div>}
        {error && <p role="alert" className="rounded-xl border border-danger/25 bg-danger-soft px-3 py-2 text-xs text-danger">{error}</p>}
      </div>

      <form onSubmit={(event) => { event.preventDefault(); void ask(draft); }} className="flex shrink-0 items-end gap-2 border-t border-line bg-white p-3">
        <label className="sr-only" htmlFor="inventory-assistant-message">Message the inventory assistant</label>
        <input ref={inputRef} id="inventory-assistant-message" value={draft} onChange={(event) => setDraft(event.target.value)} maxLength={2000} placeholder="Ask about a product or stock…" disabled={busy} className="min-w-0 flex-1 rounded-xl border border-line-strong bg-white px-3 py-2.5 text-sm outline-none focus:border-brand focus:ring-2 focus:ring-brand/15 disabled:opacity-60" />
        <button type="submit" aria-label="Send message" disabled={busy || !draft.trim()} className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-brand text-white hover:bg-brand-deep disabled:cursor-not-allowed disabled:opacity-45"><Send className="h-4 w-4" /></button>
      </form>
      <p className="shrink-0 bg-white px-4 pb-2 text-[10px] text-muted">Answers use current workspace data. The assistant cannot change inventory.</p>
    </section>}
  </>;
}
