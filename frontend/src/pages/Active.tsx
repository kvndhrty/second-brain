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
            {t.description && <p className="muted">{t.description}</p>}
            <p className="meta">
              {daysAgo(t.last_activity)} · {t.fragments} fragments
              {t.open_items > 0 && ` · ${t.open_items} open`}
            </p>
            {t.people.length > 0 && <p className="meta">{t.people.join(", ")}</p>}
          </Link>
        ))}
      </div>
    </section>
  );
}
