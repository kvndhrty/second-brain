import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, nodePath, type BriefResult } from "../api";
import {
  Badge,
  citationOrder,
  ClaimList,
  FragmentCard,
  Loading,
  SourceList,
  SummaryText,
} from "../components";
import { useAction, useLoad } from "../hooks";
import { ThreadActions } from "./ThreadActions";

const TYPE_PATH: Record<string, string> = { thread: "threads", person: "people", org: "orgs", theme: "themes" };

export function EntityListPage({ type, title }: { type: string; title: string }) {
  const { data, error, loading } = useLoad(() => api.entities(type), [type]);
  const [filter, setFilter] = useState("");
  const shown = data?.filter((e) => e.name.toLowerCase().includes(filter.toLowerCase()));
  return (
    <section>
      <h2>{title}</h2>
      <input placeholder="Filter by name" value={filter} onChange={(e) => setFilter(e.target.value)} />
      <Loading error={error} loading={loading && !data} />
      <table className="list">
        <tbody>
          {shown?.map((e) => (
            <tr key={e.id}>
              <td>
                <Link to={nodePath(e.id)}>{e.name}</Link>{" "}
                {e.state && e.state !== "active" && <Badge tone={e.state}>{e.state}</Badge>}
              </td>
              <td className="muted">{e.detail}</td>
              <td className="muted">{e.fragments} fragments</td>
              <td className="muted">{e.last_seen ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

// Shown elsewhere on the page (header, badges, related) or internal bookkeeping.
const HIDDEN_DETAILS = new Set(["description", "state", "pinned", "merged_into", "obe", "memberships", "format"]);

function shown(v: unknown): boolean {
  if (v === null || v === "" || v === false) return false;
  if (Array.isArray(v)) return v.length > 0 && v.every((x) => typeof x !== "object");
  return typeof v !== "object";
}

const KINDS = ["observation", "decision", "question", "idea", "claim", "action_item"];
const STATUSES = ["confirmed", "speculative", "overcome_by_events"];

export function EntityPage() {
  const nodeId = decodeURIComponent(useParams().nodeId ?? "");
  const entity = useLoad(() => api.entity(nodeId), [nodeId]);
  const timeline = useLoad(() => api.timeline(nodeId), [nodeId]);
  const [kinds, setKinds] = useState<string[]>([]);
  const [statuses, setStatuses] = useState<string[]>(["confirmed", "speculative"]);
  const [openOnly, setOpenOnly] = useState(false);
  const [brief, setBrief] = useState<BriefResult>();
  const { busy: briefing, error: briefError, run } = useAction();
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);

  const items = useMemo(
    () =>
      (timeline.data ?? []).filter(
        ({ fragment: f }) =>
          (kinds.length === 0 || kinds.includes(f.kind)) &&
          statuses.includes(f.status) &&
          (!openOnly || f.state === "open"),
      ),
    [timeline.data, kinds, statuses, openOnly],
  );

  const reload = () => {
    entity.reload();
    timeline.reload();
  };

  const runBrief = () => run(async () => setBrief(await api.brief(nodeId)));

  const e = entity.data;
  if (!e) return <Loading error={entity.error} loading={entity.loading} />;
  const toggle = (list: string[], value: string) =>
    list.includes(value) ? list.filter((x) => x !== value) : [...list, value];

  return (
    <section>
      <p className="muted">
        <Link to={`/${TYPE_PATH[e.type] ?? ""}`}>{e.type}</Link>
      </p>
      <h2>
        {e.details.pinned === true && "📌 "}
        {e.name}{" "}
        {typeof e.details.state === "string" && e.details.state !== "active" && (
          <Badge tone={e.details.state}>{e.details.state}</Badge>
        )}
      </h2>
      {typeof e.details.description === "string" && <p>{e.details.description}</p>}
      {e.aliases.length > 1 && <p className="muted">also: {e.aliases.join(", ")}</p>}
      <dl className="details">
        {Object.entries(e.details)
          .filter(([k, v]) => !HIDDEN_DETAILS.has(k) && shown(v))
          .map(([k, v]) => (
            <div key={k}>
              <dt>{k.replace(/_/g, " ")}</dt>
              <dd>{Array.isArray(v) ? v.join(", ") : String(v)}</dd>
            </div>
          ))}
      </dl>
      {e.notes && <p className="notes">{e.notes}</p>}
      <div className="toolbar">
        <button className="primary" onClick={() => void runBrief()} disabled={briefing}>
          {briefing ? "Writing brief…" : "Brief me"}
        </button>
        <Link className="button" to={`/graph?center=${encodeURIComponent(e.id)}`}>
          Graph
        </Link>
      </div>
      {e.type === "thread" && (
        <ThreadActions
          thread={e}
          selected={selected}
          selecting={selecting}
          setSelecting={(v) => {
            setSelecting(v);
            if (!v) setSelected([]);
          }}
          onChanged={reload}
        />
      )}
      {briefError && <p className="error">{briefError}</p>}
      {brief && <BriefPanel brief={brief} />}

      {e.summary && (
        <>
          <h3>Summary</h3>
          <SummaryText text={e.summary} />
        </>
      )}

      {Object.keys(e.related).length > 0 && (
        <div className="related">
          {Object.entries(e.related).map(([type, labels]) => (
            <p key={type}>
              <strong>{type}:</strong> {labels.slice(0, 12).join(", ")}
            </p>
          ))}
        </div>
      )}

      {e.open_items.length > 0 && (
        <>
          <h3>Open questions and action items</h3>
          {e.open_items.map((f) => (
            <FragmentCard key={f.id} fragment={f} onChanged={reload} />
          ))}
        </>
      )}

      <h3>Timeline ({e.fragment_count})</h3>
      <div className="filters">
        {KINDS.map((k) => (
          <label key={k}>
            <input type="checkbox" checked={kinds.includes(k)} onChange={() => setKinds(toggle(kinds, k))} />
            {k.replace("_", " ")}
          </label>
        ))}
        <span className="sep" />
        {STATUSES.map((s) => (
          <label key={s}>
            <input type="checkbox" checked={statuses.includes(s)} onChange={() => setStatuses(toggle(statuses, s))} />
            {s.replace(/_/g, " ")}
          </label>
        ))}
        <span className="sep" />
        <label>
          <input type="checkbox" checked={openOnly} onChange={() => setOpenOnly(!openOnly)} />
          open only
        </label>
      </div>
      <Loading error={timeline.error} loading={timeline.loading && !timeline.data} />
      {items.map(({ fragment, links }) =>
        selecting ? (
          <label key={fragment.id} className="select-row">
            <input
              type="checkbox"
              checked={selected.includes(fragment.id)}
              onChange={() => setSelected(toggle(selected, fragment.id))}
            />
            <FragmentCard fragment={fragment} links={links} onChanged={reload} />
          </label>
        ) : (
          <FragmentCard key={fragment.id} fragment={fragment} links={links} onChanged={reload} />
        ),
      )}
    </section>
  );
}

const SECTIONS: [string, string][] = [
  ["where_things_stand", "Where things stand"],
  ["recent_decisions", "Recent decisions"],
  ["open_items", "Open items"],
  ["people", "People"],
  ["speculative", "Speculative"],
  ["overcome_by_events", "Overcome by events"],
  ["changed_since", "Changed since you last looked"],
];

export function BriefPanel({ brief }: { brief: BriefResult }) {
  const all = SECTIONS.flatMap(([key]) => brief.sections[key] ?? []);
  const order = citationOrder(all);
  return (
    <div className="brief">
      <p className="muted">
        Brief on {brief.label} · {brief.fragment_count} fragments · last looked{" "}
        {brief.since ? brief.since.slice(0, 10) : "never"}
      </p>
      {brief.notes.map((n) => (
        <p key={n} className="muted">
          {n}
        </p>
      ))}
      {SECTIONS.map(([key, heading]) =>
        (brief.sections[key] ?? []).length ? (
          <div key={key}>
            <h4>{heading}</h4>
            <ClaimList claims={brief.sections[key]} order={order} />
          </div>
        ) : null,
      )}
      <h4>Sources</h4>
      <SourceList order={order} sources={brief.sources} />
    </div>
  );
}
