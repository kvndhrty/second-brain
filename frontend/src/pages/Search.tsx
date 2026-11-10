import { useState } from "react";
import { Link } from "react-router-dom";
import { api, pagePath, type AskResult, type BriefResult, type Hit } from "../api";
import { Badge, citationOrder, ClaimList, NoteLink, SourceList } from "../components";

const BRIEF_SECTIONS: [string, string][] = [
  ["where_things_stand", "Where things stand"],
  ["recent_decisions", "Recent decisions"],
  ["open_items", "Open items"],
  ["people", "People"],
  ["no_longer_applies", "No longer applies"],
  ["changed_since", "Changed since you last looked"],
];

type Kind = "search" | "ask" | "brief";

export function SearchPage() {
  const [text, setText] = useState("");
  const [hits, setHits] = useState<Hit[]>();
  const [answer, setAnswer] = useState<AskResult>();
  const [brief, setBrief] = useState<BriefResult>();
  const [busy, setBusy] = useState<Kind>();
  const [error, setError] = useState<string>();

  async function run(kind: Kind) {
    if (!text.trim()) return;
    setBusy(kind);
    setError(undefined);
    try {
      if (kind === "search") setHits(await api.search(text));
      else if (kind === "ask") setAnswer(await api.ask(text));
      else setBrief(await api.brief(text));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(undefined);
    }
  }

  const order = answer ? citationOrder(answer.claims) : [];
  const briefClaims = brief ? BRIEF_SECTIONS.flatMap(([key]) => brief.sections[key] ?? []) : [];
  const briefOrder = citationOrder(briefClaims);
  return (
    <section>
      <h2>Search &amp; ask</h2>
      <div className="toolbar">
        <input
          className="wide"
          placeholder="Words to find, a question to answer, or something to catch up on"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && void run(text.trim().endsWith("?") ? "ask" : "search")}
        />
        <button disabled={!!busy} onClick={() => void run("search")}>
          {busy === "search" ? "Searching…" : "Search"}
        </button>
        <button className="primary" disabled={!!busy} onClick={() => void run("ask")}>
          {busy === "ask" ? "Thinking…" : "Ask"}
        </button>
        <button disabled={!!busy} onClick={() => void run("brief")}>
          {busy === "brief" ? "Writing…" : "Brief me"}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {answer && (
        <div className="brief">
          {answer.insufficient && <p className="muted">The journal does not answer this directly.</p>}
          <ClaimList claims={answer.claims} order={order} />
          <SourceList order={order} sources={answer.sources} />
        </div>
      )}
      {brief && (
        <div className="brief">
          <h3>{brief.topics.map((t) => t.name).join(", ") || brief.request}</h3>
          {briefClaims.length === 0 && <p className="muted">Nothing in the journal about this yet.</p>}
          {BRIEF_SECTIONS.map(([key, title]) =>
            (brief.sections[key] ?? []).length === 0 ? null : (
              <div key={key}>
                <h4>{title}</h4>
                <ClaimList claims={brief.sections[key]} order={briefOrder} />
              </div>
            ),
          )}
          <SourceList order={briefOrder} sources={brief.sources} />
        </div>
      )}
      {hits && hits.length === 0 && <p>No matches.</p>}
      {hits?.map((h) => (
        <div key={`${h.type}:${h.id}`} className="hit">
          <Badge tone={h.kind ?? h.type}>{h.kind ?? h.type}</Badge>{" "}
          {h.type === "note" || h.type === "statement" ? (
            <NoteLink id={h.note_id ?? h.id}>{h.date ?? h.label}</NoteLink>
          ) : (
            <Link to={pagePath(h.id)}>{h.label}</Link>
          )}
          <p>{h.snippet || (h.type === "note" ? h.label : "")}</p>
        </div>
      ))}
    </section>
  );
}
