import { useState } from "react";
import { Link } from "react-router-dom";
import { api, day, minute, pagePath } from "../api";
import { Loading, NoteBox, NoteLink } from "../components";
import { useInterval, useLoad } from "../hooks";

function daysAgo(iso: string | null): string {
  if (!iso) return "no activity yet";
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000);
  return days <= 0 ? "today" : days === 1 ? "yesterday" : `${days} days ago`;
}

export function HomePage({ onSaved }: { onSaved?: () => void }) {
  const active = useLoad(() => api.active());
  const log = useLoad(() => api.log(15));
  // A saved note is processed in the background; refresh while that may be happening.
  const [watch, setWatch] = useState(0);
  useInterval(
    () => {
      active.reload();
      log.reload();
      setWatch((n) => n - 1);
    },
    3000,
    watch > 0,
  );
  return (
    <section>
      <NoteBox
        autoFocus
        onSaved={() => {
          setWatch(20);
          onSaved?.();
        }}
      />
      <h2>Active threads</h2>
      <Loading error={active.error} loading={active.loading && !active.data} />
      {active.data && active.data.length === 0 && (
        <p className="muted">Nothing yet. Write a note above; brain builds the wiki from what you write.</p>
      )}
      <div className="cards">
        {active.data?.map((t) => (
          <Link key={t.id} to={pagePath(t.id)} className="card">
            <h3>{t.name}</h3>
            {t.now && <p>{t.now}</p>}
            <p className="meta">
              {daysAgo(t.last_activity)}
              {t.open_items > 0 && ` · ${t.open_items} open`}
            </p>
          </Link>
        ))}
      </div>
      <h2>What changed</h2>
      {log.data && log.data.length === 0 && <p className="muted">Nothing yet.</p>}
      <ul className="log">
        {log.data?.map((ev, i) => (
          <li key={i}>
            <span className="meta">{day(ev.at)}</span> {ev.text}{" "}
            {ev.source.startsWith("n-") ? <NoteLink id={ev.source}>note</NoteLink> : <span className="meta">dreaming</span>}
          </li>
        ))}
      </ul>
      {log.data && log.data.length > 0 && <Link to="/log">Full change log</Link>}
      <DreamPanel />
      <ContextPanel />
    </section>
  );
}

// What dreaming last did, and a button to ask the background worker for a pass.
function DreamPanel() {
  const { data, reload } = useLoad(() => api.dreamStatus());
  const [note, setNote] = useState<string | null>(null);
  if (!data) return null;
  const last = data.last;
  const waiting = Object.values(data.planned).reduce((n, items) => n + items.length, 0);
  async function dreamNow() {
    try {
      await api.dreamNow();
      setNote("Requested; the background worker dreams when it is idle.");
      setTimeout(reload, 5000);
    } catch (e) {
      setNote(e instanceof Error ? e.message : String(e));
    }
  }
  return (
    <details className="card" data-testid="dream-panel">
      <summary>
        Dreaming:{" "}
        {last
          ? `last pass ${minute(last.finished_at ?? last.started_at)}, ${last.events.length} change(s)`
          : "no pass yet"}
        {waiting > 0 && ` · ${waiting} thing(s) to look at`}
      </summary>
      {last && last.errors.length > 0 && <p className="error">{last.errors[last.errors.length - 1]}</p>}
      {last && last.events.length > 0 && (
        <ul>
          {last.events.map((ev, i) => (
            <li key={i}>{ev.text}</li>
          ))}
        </ul>
      )}
      <button onClick={() => void dreamNow()}>Dream now</button>
      {note && <p className="muted">{note}</p>}
    </details>
  );
}

// The injected context: exactly what an agent receives at the start of a session.
function ContextPanel() {
  const { data } = useLoad(() => api.context());
  if (!data) return null;
  return (
    <details className="card" data-testid="context-panel">
      <summary>What an agent sees at the start of a session</summary>
      <pre>{data.text}</pre>
    </details>
  );
}
