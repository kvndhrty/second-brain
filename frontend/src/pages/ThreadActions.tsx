import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, nodePath, type EntityView, type ThreadOp } from "../api";
import { EntitySearch } from "../components";
import { useAction } from "../hooks";

// Pin, archive, retire, rename, merge, and split a thread. Splitting moves the
// fragments selected in the timeline into a new thread.
export function ThreadActions({
  thread,
  selected,
  selecting,
  setSelecting,
  onChanged,
}: {
  thread: EntityView;
  selected: string[];
  selecting: boolean;
  setSelecting: (v: boolean) => void;
  onChanged: () => void;
}) {
  const navigate = useNavigate();
  const id = thread.id.slice("thread:".length);
  const state = String(thread.details.state ?? "active");
  const pinned = thread.details.pinned === true;
  const { busy, error, run: act } = useAction();
  const [panel, setPanel] = useState<"" | "obe" | "rename" | "merge" | "split">("");
  const [text, setText] = useState("");
  const [cascade, setCascade] = useState(true);

  const run = (op: ThreadOp, after?: (result: { id: string }) => void) =>
    act(async () => {
      const result = await api.threadOp(id, op);
      setPanel("");
      setText("");
      if (after) after(result);
      else onChanged();
    });

  const open = (p: typeof panel, initial = "") => {
    setPanel(panel === p ? "" : p);
    setText(initial);
    setSelecting(p === "split");
  };

  if (state === "merged")
    return (
      <p className="muted">
        Merged into{" "}
        <Link to={nodePath(`thread:${thread.details.merged_into}`)}>{String(thread.details.merged_into)}</Link>.
      </p>
    );

  return (
    <div className="thread-actions">
      <div className="toolbar">
        <button disabled={busy} onClick={() => void run({ op: pinned ? "unpin" : "pin" })}>
          {pinned ? "Unpin" : "Pin"}
        </button>
        {state === "proposed" && (
          <button className="primary" disabled={busy} onClick={() => void run({ op: "activate" })}>
            Accept thread
          </button>
        )}
        {state === "active" || state === "proposed" ? (
          <>
            <button disabled={busy} onClick={() => void run({ op: "archive" })}>
              Archive
            </button>
            <button disabled={busy} onClick={() => open("obe")}>
              Overcome by events…
            </button>
          </>
        ) : (
          <button disabled={busy} onClick={() => void run({ op: "activate" })}>
            Make active again
          </button>
        )}
        <button disabled={busy} onClick={() => open("rename", thread.name)}>
          Rename…
        </button>
        <button disabled={busy} onClick={() => open("merge")}>
          Merge into…
        </button>
        <button disabled={busy} onClick={() => open("split")}>
          {selecting ? "Choosing fragments…" : "Split…"}
        </button>
      </div>
      {panel === "obe" && (
        <div className="toolbar">
          <input className="wide" placeholder="Why it no longer applies (optional)" value={text} onChange={(e) => setText(e.target.value)} />
          <label>
            <input type="checkbox" checked={cascade} onChange={() => setCascade(!cascade)} /> also its open items
          </label>
          <button className="primary" disabled={busy} onClick={() => void run({ op: "obe", reason: text || undefined, cascade })}>
            Mark overcome by events
          </button>
        </div>
      )}
      {panel === "rename" && (
        <div className="toolbar">
          <input className="wide" aria-label="new name" value={text} onChange={(e) => setText(e.target.value)} />
          <button className="primary" disabled={busy || !text.trim()} onClick={() => void run({ op: "rename", name: text })}>
            Rename
          </button>
        </div>
      )}
      {panel === "merge" && (
        <EntitySearch
          type="thread"
          label="Merge into"
          exclude={id}
          busy={busy}
          onPick={(into) => void run({ op: "merge", into }, (r) => navigate(nodePath(`thread:${r.id}`)))}
        />
      )}
      {panel === "split" && (
        <div className="toolbar">
          <span className="muted">{selected.length} selected in the timeline below</span>
          <input placeholder="Name of the new thread" value={text} onChange={(e) => setText(e.target.value)} />
          <button
            className="primary"
            disabled={busy || !text.trim() || selected.length === 0}
            onClick={() =>
              void run({ op: "split", name: text, fragment_ids: selected }, (r) => {
                setSelecting(false);
                navigate(nodePath(`thread:${r.id}`));
              })
            }
          >
            Split into new thread
          </button>
        </div>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
