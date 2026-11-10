import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, day, pagePath, type PageSummary } from "../api";
import { Badge, CitedText, Loading, NoteBox, PageChip, StatementLine } from "../components";
import { useLoad } from "../hooks";

const GROUPS: [string, string][] = [
  ["thread", "Threads"],
  ["person", "People"],
  ["org", "Organizations"],
  ["topic", "Topics"],
];

export function WikiPage() {
  const { data, error, loading } = useLoad(() => api.pages());
  const of = (type: string): PageSummary[] => (data ?? []).filter((p) => p.type === type);
  return (
    <section>
      <h2>Wiki</h2>
      <p className="muted">
        Written by brain from your notes. To change a page, open it and use “This is wrong”, or just write a note.
      </p>
      <Loading error={error} loading={loading && !data} />
      {data && data.length === 0 && <p>No pages yet.</p>}
      {GROUPS.map(([type, title]) =>
        of(type).length === 0 ? null : (
          <div key={type}>
            <h3>{title}</h3>
            <table className="list">
              <tbody>
                {of(type).map((p) => (
                  <tr key={p.id}>
                    <td>
                      <Link to={pagePath(p.id)}>{p.name}</Link> {p.status === "closed" && <Badge>closed</Badge>}
                    </td>
                    <td className="muted">{p.now}</td>
                    <td className="meta">{day(p.last_activity)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ),
      )}
    </section>
  );
}

export function EntityPage() {
  const { id = "" } = useParams();
  const page = useLoad(() => api.page(id), [id]);
  const [correcting, setCorrecting] = useState(false);
  const p = page.data;
  if (!p) return <Loading error={page.error} loading={page.loading} />;
  const facts = Object.entries(p.profile).filter(([, v]) => v);
  return (
    <section>
      <div className="toolbar">
        <h2>
          {p.name} <Badge tone={p.type}>{p.type}</Badge> {p.status === "closed" && <Badge>closed</Badge>}
        </h2>
        <button onClick={() => setCorrecting(!correcting)}>This is wrong</button>
      </div>
      {correcting && (
        <NoteBox
          about={{ id: p.id, type: p.type, name: p.name }}
          autoFocus
          placeholder="What is wrong, in your own words"
          onCancel={() => setCorrecting(false)}
          onSaved={() => setTimeout(page.reload, 4000)}
        />
      )}
      <p className="meta">
        {facts.map(([k, v]) => `${k}: ${v}`).join(" · ")}
        {facts.length > 0 && p.names.length > 0 && " · "}
        {p.names.length > 0 && `also called ${p.names.join(", ")}`}
        {p.as_of && ` · as of ${p.as_of}`}
        {p.status === "closed" && ` · closed ${p.closed_at ?? ""}; everything below is history`}
      </p>
      {p.now && (
        <>
          <h3>Now</h3>
          <p>
            <CitedText cited={p.now} />
          </p>
        </>
      )}
      {p.summary.length > 0 && (
        <>
          <h3>Summary</h3>
          <p className="summary">
            {p.summary.map((s, i) => (
              <span key={i}>
                <CitedText cited={s} />{" "}
              </span>
            ))}
          </p>
        </>
      )}
      {p.open.length > 0 && (
        <>
          <h3>Open</h3>
          <ul className="statements">
            {p.open.map((s) => (
              <StatementLine key={s.id} statement={s} showThread={p.type !== "thread"} />
            ))}
          </ul>
        </>
      )}
      {p.decided.length > 0 && (
        <>
          <h3>Decided</h3>
          <ul className="statements">
            {p.decided.map((s) => (
              <StatementLine key={s.id} statement={s} />
            ))}
          </ul>
        </>
      )}
      {p.related.length > 0 && (
        <>
          <h3>Related</h3>
          <p className="related">
            {p.related.map((r) => (
              <PageChip key={r.id} page={r} />
            ))}
          </p>
        </>
      )}
      {p.history.length > 0 && (
        <>
          <h3>History</h3>
          <ul className="statements">
            {p.history.map((s) => (
              <StatementLine key={s.id} statement={s} showThread={p.type !== "thread"} />
            ))}
          </ul>
        </>
      )}
      {p.log.length > 0 && (
        <details>
          <summary>What brain did to this page</summary>
          <ul className="log">
            {p.log.map((ev, i) => (
              <li key={i}>
                <span className="meta">{day(ev.at)}</span> {ev.text}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
