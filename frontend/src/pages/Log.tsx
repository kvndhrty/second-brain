import { Link } from "react-router-dom";
import { api, day, pagePath } from "../api";
import { Loading, NoteLink } from "../components";
import { useLoad } from "../hooks";

export function LogPage() {
  const { data, error, loading } = useLoad(() => api.log(300));
  return (
    <section>
      <h2>Change log</h2>
      <p className="muted">
        What brain did after each note and in each dream pass, newest first. If something here is wrong, write a note
        saying so.
      </p>
      <Loading error={error} loading={loading && !data} />
      <ul className="log">
        {data?.map((ev, i) => (
          <li key={i}>
            <span className="meta">{day(ev.at)}</span>{" "}
            {ev.entity_id ? <Link to={pagePath(ev.entity_id)}>{ev.text}</Link> : ev.text}{" "}
            {ev.source.startsWith("n-") ? <NoteLink id={ev.source}>note</NoteLink> : <span className="meta">dreaming</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}
