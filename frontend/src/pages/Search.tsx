import { useState } from "react";
import { Link } from "react-router-dom";
import { api, nodePath, type AskResult, type Hit } from "../api";
import { Badge, citationOrder, ClaimList, EntryLink, SourceList } from "../components";

export function SearchPage() {
  const [text, setText] = useState("");
  const [hits, setHits] = useState<Hit[]>();
  const [answer, setAnswer] = useState<AskResult>();
  const [busy, setBusy] = useState<"search" | "ask">();
  const [error, setError] = useState<string>();

  async function run(kind: "search" | "ask") {
    if (!text.trim()) return;
    setBusy(kind);
    setError(undefined);
    try {
      if (kind === "search") setHits(await api.search(text));
      else setAnswer(await api.ask(text));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(undefined);
    }
  }

  const order = answer ? citationOrder(answer.claims) : [];
  return (
    <section>
      <h2>Search &amp; ask</h2>
      <div className="toolbar">
        <input
          className="wide"
          placeholder="Words to find, or a question to answer"
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
      </div>
      {error && <p className="error">{error}</p>}
      {answer && (
        <div className="brief">
          {answer.insufficient && <p className="muted">The journal does not answer this directly.</p>}
          <ClaimList claims={answer.claims} order={order} />
          <SourceList order={order} sources={answer.sources} />
        </div>
      )}
      {hits && hits.length === 0 && <p>No matches.</p>}
      {hits?.map((h) => (
        <div key={`${h.type}:${h.id}`} className="hit">
          <Badge tone={h.kind ?? h.type}>{h.kind ?? h.type}</Badge>{" "}
          {h.type === "entry" || h.type === "fragment" ? (
            <EntryLink id={h.entry_id ?? h.id}>{h.date ?? h.label}</EntryLink>
          ) : (
            <Link to={nodePath(h.id)}>{h.label}</Link>
          )}
          <p>{h.snippet}</p>
        </div>
      ))}
    </section>
  );
}
