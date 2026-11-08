import { useState } from "react";
import { Link } from "react-router-dom";
import { api, nodePath } from "../api";
import { Badge, Loading } from "../components";
import { useLoad } from "../hooks";

function daysAgo(iso: string | null): string {
  if (!iso) return "no activity yet";
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000);
  return days <= 0 ? "today" : days === 1 ? "yesterday" : `${days} days ago`;
}

export function ActivePage() {
  const { data, error, loading } = useLoad(() => api.active());
  return (
    <section>
      <h2>Active threads</h2>
      <p className="muted">Threads with recent activity, plus pinned ones. Others fade out after a quiet period.</p>
      <Loading error={error} loading={loading && !data} />
      {data && data.length === 0 && (
        <p>
          Nothing active yet. <Link to="/entries/new">Write an entry</Link> to get started.
        </p>
      )}
      <div className="cards">
        {data?.map((t) => (
          <Link key={t.id} to={nodePath(`thread:${t.id}`)} className="card">
            <h3>
              {t.pinned && "📌 "}
              {t.name} {t.state === "proposed" && <Badge tone="proposed">proposed</Badge>}
            </h3>
            {t.now_line ? (
              <p>
                {t.now_line} <span className="meta">(as of {t.now_as_of?.slice(0, 10) ?? "unknown"})</span>
              </p>
            ) : (
              t.description && <p className="muted">{t.description}</p>
            )}
            <p className="meta">
              {daysAgo(t.last_activity)} · {t.fragments} fragments
              {t.open_items > 0 && ` · ${t.open_items} open`}
            </p>
            {t.people.length > 0 && <p className="meta">{t.people.join(", ")}</p>}
          </Link>
        ))}
      </div>
      <DreamPanel />
      <ContextPanel />
    </section>
  );
}

// What `brain dream` last did, and a button to ask the background worker for a pass.
function DreamPanel() {
  const { data, reload } = useLoad(() => api.dreamStatus());
  const [note, setNote] = useState<string | null>(null);
  if (!data) return null;
  const last = data.last;
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
          ? `last pass ${(last.finished_at ?? last.started_at).slice(0, 16).replace("T", " ")}, ${last.proposed.length} proposed, ${last.applied.length} applied`
          : "no pass yet"}
        {data.planned.length > 0 && ` · ${data.planned.length} step(s) waiting`}
      </summary>
      {last && last.errors.length > 0 && <p className="error">{last.errors[last.errors.length - 1]}</p>}
      {data.planned.length > 0 && (
        <ul>
          {data.planned.map((p) => (
            <li key={p}>{p}</li>
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
