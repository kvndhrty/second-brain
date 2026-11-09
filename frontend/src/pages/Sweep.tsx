import { useState } from "react";
import { Link } from "react-router-dom";
import { api, nodePath, type FragmentPatch, type SweepItem } from "../api";
import { askObeReason, EntryLink, Loading } from "../components";
import { useAction, useLoad } from "../hooks";

// The overcome-by-events sweep: old open items, old speculation, and quiet threads.
export function SweepPage() {
  const [days, setDays] = useState(30);
  const { data, error, loading, reload } = useLoad(() => api.sweep(days), [days]);
  return (
    <section>
      <h2>Tidy up</h2>
      <p className="muted">
        Things that may no longer hold: questions and action items still open, speculation never confirmed, and
        threads that have gone quiet. Items you have already decided on are not shown again.
      </p>
      <label>
        Older than{" "}
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
          {[7, 14, 30, 60, 90].map((d) => (
            <option key={d} value={d}>
              {d} days
            </option>
          ))}
        </select>
      </label>
      <Loading error={error} loading={loading && !data} />
      {data?.length === 0 && <p>Nothing stale.</p>}
      {data?.map((item) => (
        <SweepCard key={item.fragment?.id ?? item.thread_id} item={item} onDone={reload} />
      ))}
    </section>
  );
}

function SweepCard({ item, onDone }: { item: SweepItem; onDone: () => void }) {
  const { busy, error, run } = useAction(true);
  const act = (fn: () => Promise<unknown>) =>
    run(async () => {
      await fn();
      onDone();
    });
  const obeReason = askObeReason;

  if (item.kind === "quiet_thread") {
    const id = item.thread_id ?? "";
    return (
      <article className="card review" data-testid="sweep-item">
        <p>
          Thread <Link to={nodePath(`thread:${id}`)}>{item.thread_name}</Link> has been quiet for {item.age_days} days.
        </p>
        <div className="toolbar">
          <button disabled={busy} onClick={() => void act(() => api.threadOp(id, { op: "archive" }))}>
            Archive
          </button>
          <button
            disabled={busy}
            onClick={() => {
              const reason = obeReason();
              if (reason !== null) void act(() => api.threadOp(id, { op: "obe", reason: reason || undefined, cascade: true }));
            }}
          >
            Overcome by events
          </button>
          <button disabled={busy} onClick={() => void act(() => api.threadOp(id, { op: "pin" }))}>
            Pin (keep active)
          </button>
        </div>
        {error && <p className="error">{error}</p>}
      </article>
    );
  }

  const f = item.fragment!;
  const patch = (p: FragmentPatch) => act(() => api.patchFragment(f.id, p));
  const open = item.kind === "stale_open";
  return (
    <article className="card review" data-testid="sweep-item">
      <p className="muted">
        {open ? "Still open" : "Still unconfirmed"} after {item.age_days} days ·{" "}
        <EntryLink id={f.entry_id}>{f.date}</EntryLink>
        {item.threads?.length ? ` · ${item.threads.join(", ")}` : ""}
      </p>
      <p>{f.text}</p>
      {item.suggestion && (
        <p className="muted" data-testid="sweep-suggestion">
          Dreaming says: <strong>{item.suggestion.verdict.replace("_", " ")}</strong>. {item.suggestion.reason}
        </p>
      )}
      <div className="toolbar">
        <button
          disabled={busy}
          onClick={() => {
            const reason = obeReason();
            if (reason !== null) void patch({ status: "overcome_by_events", obe_reason: reason || null });
          }}
        >
          Overcome by events
        </button>
        {open ? (
          <>
            <button disabled={busy} onClick={() => void patch({ state: "closed" })}>
              Done / answered
            </button>
            <button disabled={busy} onClick={() => void patch({ state: "open" })}>
              Keep open
            </button>
          </>
        ) : (
          <>
            <button disabled={busy} onClick={() => void patch({ status: "confirmed" })}>
              Confirmed
            </button>
            <button disabled={busy} onClick={() => void patch({ status: "speculative" })}>
              Keep as speculative
            </button>
          </>
        )}
      </div>
      {error && <p className="error">{error}</p>}
    </article>
  );
}
