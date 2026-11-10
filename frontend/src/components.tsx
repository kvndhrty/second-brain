import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, notePath, pagePath, type Claim, type Cited, type Ref, type Source, type Statement } from "./api";
import { useAction } from "./hooks";

export function Loading({ error, loading }: { error?: string; loading: boolean }) {
  if (error) return <p className="error">{error}</p>;
  if (loading) return <p className="muted">Loading…</p>;
  return null;
}

const KIND_LABEL: Record<string, string> = {
  fact: "",
  decision: "decided",
  question: "question",
  action: "to do",
  idea: "idea",
  correction: "correction",
};

export function Badge({ children, tone }: { children: ReactNode; tone?: string }) {
  return <span className={`badge ${tone ?? ""}`}>{children}</span>;
}

export function NoteLink({ id, children }: { id: string; children?: ReactNode }) {
  return <Link to={notePath(id)}>{children ?? id}</Link>;
}

export function PageChip({ page }: { page: Ref }) {
  return (
    <Link className={`chip ${page.type}`} to={pagePath(page.id)}>
      {page.name}
    </Link>
  );
}

// The one place a person writes. With `about`, the note is a correction to that wiki page.
export function NoteBox({
  about,
  placeholder = "What happened, what was decided, what is still open…",
  autoFocus = false,
  onSaved,
  onCancel,
}: {
  about?: Ref;
  placeholder?: string;
  autoFocus?: boolean;
  onSaved?: (id: string) => void;
  onCancel?: () => void;
}) {
  const [text, setText] = useState("");
  const [saved, setSaved] = useState<string>();
  const { busy, error, run } = useAction();
  const submit = () =>
    run(async () => {
      const { id } = await api.jot(text, about?.id);
      setText("");
      setSaved(id);
      onSaved?.(id);
    });

  return (
    <div className="notebox">
      {about && (
        <p className="muted">
          Say what is wrong with <strong>{about.name}</strong> in your own words, for example “this is the same
          person as …”, “this is finished”, or “the December date is wrong, it moved to January”.
        </p>
      )}
      <textarea
        aria-label="note"
        autoFocus={autoFocus}
        placeholder={placeholder}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && text.trim()) void submit();
        }}
        rows={about ? 3 : 5}
      />
      <div className="toolbar">
        <button className="primary" disabled={busy || !text.trim()} onClick={() => void submit()}>
          {busy ? "Saving…" : about ? "Save correction" : "Save note"}
        </button>
        {onCancel && <button onClick={onCancel}>Cancel</button>}
        <span className="muted">⌘/Ctrl + Enter to save</span>
        {saved && (
          <span className="muted">
            Saved. <NoteLink id={saved}>See what brain made of it</NoteLink>
          </span>
        )}
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

// One statement as a line on a page: its date, kind, text, and what replaced it.
export function StatementLine({ statement: s, showThread = false }: { statement: Statement; showThread?: boolean }) {
  const label = KIND_LABEL[s.kind] ?? s.kind;
  return (
    <li className={`statement ${s.status}`} data-testid="statement">
      <NoteLink id={s.note_id}>{s.date}</NoteLink>{" "}
      {label && <Badge tone={s.kind}>{label}</Badge>} <span className="text">{s.text}</span>
      {s.status === "superseded" && <span className="muted"> Replaced{s.replaced_by ? ` by: ${s.replaced_by}` : ""}</span>}
      {s.status === "answered" && <span className="muted"> Answered{s.replaced_by ? `: ${s.replaced_by}` : ""}</span>}
      {showThread && s.thread && (
        <>
          {" "}
          <PageChip page={{ id: s.thread, type: "thread", name: s.thread_name ?? s.thread }} />
        </>
      )}
    </li>
  );
}

export function CitedText({ cited }: { cited: Cited }) {
  return (
    <span>
      {cited.text}{" "}
      {cited.citations.map((id) => (
        <sup key={id}>
          <NoteLink id={id}>[{id.slice(2, 10)}]</NoteLink>
        </sup>
      ))}
    </span>
  );
}

// Numbers sources in the order they are first cited, like footnotes.
export function citationOrder(claims: Claim[]): string[] {
  const order: string[] = [];
  for (const c of claims) for (const id of c.citations) if (!order.includes(id)) order.push(id);
  return order;
}

export function ClaimList({ claims, order }: { claims: Claim[]; order: string[] }) {
  return (
    <ul className="claims">
      {claims.map((c, i) => (
        <li key={i}>
          {c.text}{" "}
          {c.citations.map((id) => (
            <sup key={id}>
              <NoteLink id={id}>[{order.indexOf(id) + 1}]</NoteLink>
            </sup>
          ))}
        </li>
      ))}
    </ul>
  );
}

export function SourceList({ order, sources }: { order: string[]; sources: Source[] }) {
  if (order.length === 0) return null;
  const byId = new Map(sources.map((s) => [s.note_id, s]));
  return (
    <ol className="sources">
      {order.map((id) => {
        const s = byId.get(id);
        return (
          <li key={id}>
            <NoteLink id={id}>{s ? `${s.date} · ${s.label}` : id}</NoteLink>
          </li>
        );
      })}
    </ol>
  );
}
