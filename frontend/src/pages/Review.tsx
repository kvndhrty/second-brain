import { useState } from "react";
import { api, type Candidate, type ReviewDecision, type ReviewItem } from "../api";
import { EntryLink, Loading } from "../components";
import { useLoad } from "../hooks";

const TITLES: Record<string, string> = {
  new_person: "New person",
  new_org: "New organization",
  ambiguous_mention: "Which one is this?",
  proposed_theme: "Proposed theme",
  proposed_thread: "Proposed thread",
  proposed_answer: "Does this answer an earlier item?",
  orphaned_decision: "A decision no longer matches any fragment",
};

export function ReviewPage({ onChange }: { onChange?: () => void }) {
  const { data, error, loading, reload } = useLoad(() => api.review());
  const refresh = () => {
    reload();
    onChange?.();
  };
  return (
    <section>
      <h2>Review</h2>
      <p className="muted">
        New people and organizations wait here before they are created, because duplicates are hard to undo.
        Proposed themes and threads are already in use; accept, rename, or merge them.
      </p>
      <Loading error={error} loading={loading && !data} />
      {data?.length === 0 && <p>Nothing to review.</p>}
      {data?.map((item) => (
        <ReviewCard key={item.id} item={item} onDone={refresh} />
      ))}
    </section>
  );
}

function ReviewCard({ item, onDone }: { item: ReviewItem; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [applyAll, setApplyAll] = useState(true);

  async function decide(decision: ReviewDecision) {
    setBusy(true);
    setError(undefined);
    try {
      await api.resolve(item.id, { apply_to_all: applyAll, ...decision });
      onDone();
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  const p = item.payload;
  return (
    <article className="card review" data-testid="review-item">
      <h3>{TITLES[item.kind] ?? item.kind}</h3>
      {item.entry_id && (
        <p className="muted">
          from <EntryLink id={item.entry_id}>{item.entry_date ?? item.entry_id}</EntryLink>
          {item.entry_label && ` · ${item.entry_label.slice(0, 80)}`}
        </p>
      )}
      {["new_person", "new_org", "ambiguous_mention"].includes(item.kind) && (
        <MentionReview item={item} busy={busy} decide={decide} applyAll={applyAll} setApplyAll={setApplyAll} />
      )}
      {(item.kind === "proposed_theme" || item.kind === "proposed_thread") && (
        <ProposedReview type={item.kind === "proposed_theme" ? "theme" : "thread"} item={item} busy={busy} decide={decide} />
      )}
      {item.kind === "proposed_answer" && (
        <>
          <p>
            <strong>Earlier:</strong> {item.context[0]}
          </p>
          <p>
            <strong>New:</strong> {item.context[1]}
          </p>
          <div className="toolbar">
            <button className="primary" disabled={busy} onClick={() => void decide({ action: "yes" })}>
              Yes, close the earlier item
            </button>
            <button disabled={busy} onClick={() => void decide({ action: "no" })}>
              No
            </button>
          </div>
        </>
      )}
      {item.kind === "orphaned_decision" && (
        <>
          <pre>{JSON.stringify(p.decision, null, 2)}</pre>
          <button disabled={busy} onClick={() => void decide({ action: "drop" })}>
            Drop this decision
          </button>
        </>
      )}
      {error && <p className="error">{error}</p>}
    </article>
  );
}

function MentionReview({
  item,
  busy,
  decide,
  applyAll,
  setApplyAll,
}: {
  item: ReviewItem;
  busy: boolean;
  decide: (d: ReviewDecision) => Promise<void>;
  applyAll: boolean;
  setApplyAll: (v: boolean) => void;
}) {
  const p = item.payload;
  const type: string = p.type;
  const [name, setName] = useState<string>(p.proposed?.name ?? p.surface);
  const [role, setRole] = useState("");
  const candidates: Candidate[] = p.candidates ?? [];
  return (
    <>
      <p>
        <strong className="mention">{p.surface}</strong> <span className="muted">({type})</span>
      </p>
      {item.context.slice(0, 3).map((c, i) => (
        <blockquote key={i}>{c}</blockquote>
      ))}
      {candidates.length > 0 && (
        <div className="toolbar">
          {candidates.map((c) => (
            <button key={c.entity_id} disabled={busy} onClick={() => void decide({ action: "link", entity_id: c.entity_id })}>
              It is {c.entity_id} <span className="muted">({c.score})</span>
            </button>
          ))}
        </div>
      )}
      <EntitySearch type={type} label="Link to existing" busy={busy} onPick={(id) => void decide({ action: "link", entity_id: id })} />
      <div className="toolbar">
        <input aria-label="name" value={name} onChange={(e) => setName(e.target.value)} />
        {type === "person" && <input placeholder="Role (optional)" value={role} onChange={(e) => setRole(e.target.value)} />}
        <button className="primary" disabled={busy || !name.trim()} onClick={() => void decide({ action: "create", name, role: role || undefined })}>
          Create {type}
        </button>
        <button disabled={busy} onClick={() => void decide({ action: "ignore" })}>
          Not a {type}
        </button>
      </div>
      {item.others > 0 && (
        <label>
          <input type="checkbox" checked={applyAll} onChange={() => setApplyAll(!applyAll)} />
          also apply to {item.others} other {item.others === 1 ? "entry" : "entries"} with this name
        </label>
      )}
    </>
  );
}

function ProposedReview({
  type,
  item,
  busy,
  decide,
}: {
  type: "theme" | "thread";
  item: ReviewItem;
  busy: boolean;
  decide: (d: ReviewDecision) => Promise<void>;
}) {
  const [name, setName] = useState<string>(item.payload.name);
  const self: string = item.payload[type];
  return (
    <>
      <div className="toolbar">
        <input aria-label="name" value={name} onChange={(e) => setName(e.target.value)} />
        <button
          className="primary"
          disabled={busy || !name.trim()}
          onClick={() => void decide(name === item.payload.name ? { action: "accept" } : { action: "rename", name })}
        >
          {name === item.payload.name ? "Accept" : "Rename and accept"}
        </button>
      </div>
      <EntitySearch type={type} label="Merge into" exclude={self} busy={busy} onPick={(id) => void decide({ action: "merge", into: id })} />
    </>
  );
}

function EntitySearch({
  type,
  label,
  busy,
  exclude,
  onPick,
}: {
  type: string;
  label: string;
  busy: boolean;
  exclude?: string;
  onPick: (id: string) => void;
}) {
  const [text, setText] = useState("");
  const [found, setFound] = useState<Candidate[]>();
  return (
    <div className="toolbar">
      <input placeholder={`${label}: search ${type}s`} value={text} onChange={(e) => setText(e.target.value)} />
      <button disabled={busy || !text.trim()} onClick={() => void api.choices(type, text).then(setFound)}>
        Search
      </button>
      {found?.filter((c) => c.entity_id !== exclude).map((c) => (
        <button key={c.entity_id} disabled={busy} onClick={() => onPick(c.entity_id)}>
          {label} {c.name ?? c.entity_id}
        </button>
      ))}
      {found && found.length === 0 && <span className="muted">no match</span>}
    </div>
  );
}
