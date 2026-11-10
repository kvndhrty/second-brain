import { Link, useParams } from "react-router-dom";
import { api, day, minute, notePath } from "../api";
import { Badge, Loading, PageChip, StatementLine } from "../components";
import { useLoad } from "../hooks";

export function NotesPage() {
  const { data, error, loading } = useLoad(() => api.notes());
  return (
    <section>
      <h2>Notes</h2>
      <p className="muted">Everything you wrote, newest first. Notes are never changed.</p>
      <Loading error={error} loading={loading && !data} />
      {data && data.length === 0 && <p>No notes yet.</p>}
      {data?.map((n) => (
        <Link key={n.id} to={notePath(n.id)} className="entry-row">
          <span className="meta">{day(n.date)}</span>
          <span>{n.label}</span>
          <span className="meta">{n.done ? `${n.statements} statements` : "processing"}</span>
        </Link>
      ))}
    </section>
  );
}

export function NotePage() {
  const { id = "" } = useParams();
  const { data: n, error, loading } = useLoad(() => api.note(id), [id]);
  if (!n) return <Loading error={error} loading={loading} />;
  return (
    <section>
      <h2>{n.title ?? day(n.created)}</h2>
      <p className="meta">
        {minute(n.created)} · {n.id} {!n.done && <Badge tone="pending">processing</Badge>}
      </p>
      <pre className="entry-text">{n.body}</pre>
      {n.statements.length > 0 && (
        <>
          <h3>What brain made of it</h3>
          <ul className="statements">
            {n.statements.map((s) => (
              <StatementLine key={s.id} statement={s} showThread />
            ))}
          </ul>
        </>
      )}
      {n.entities.length > 0 && (
        <p className="related">
          {n.entities.map((e) => (
            <PageChip key={e.id} page={e} />
          ))}
        </p>
      )}
      {n.events.length > 0 && (
        <ul className="log">
          {n.events.map((ev, i) => (
            <li key={i}>{ev.text}</li>
          ))}
        </ul>
      )}
    </section>
  );
}
