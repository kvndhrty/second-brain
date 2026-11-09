import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { Badge, FragmentCard, Loading } from "../components";
import { useAction, useInterval, useLoad } from "../hooks";

export function EntriesPage() {
  const { data, error, loading } = useLoad(() => api.entries());
  return (
    <section>
      <h2>Entries</h2>
      <Loading error={error} loading={loading && !data} />
      {data?.map((e) => (
        <Link key={e.id} to={`/entries/${encodeURIComponent(e.id)}`} className="entry-row">
          <span className="muted">{e.date.replace("T", " ")}</span>
          <strong>{e.title ?? ""}</strong>
          <span>{e.preview}</span>
          {!e.processed && <Badge tone="pending">processing</Badge>}
        </Link>
      ))}
    </section>
  );
}

export function EntryPage() {
  const id = decodeURIComponent(useParams().id ?? "");
  const entry = useLoad(() => api.entry(id), [id]);
  const [editing, setEditing] = useState(false);
  const e = entry.data;
  // New and edited entries are processed in the background; refresh until done.
  useInterval(entry.reload, 2000, !!e && !e.processed);

  if (!e) return <Loading error={entry.error} loading={entry.loading} />;
  if (editing)
    return (
      <EntryForm
        initialTitle={e.title ?? ""}
        initialText={e.text}
        submitLabel="Save changes"
        onCancel={() => setEditing(false)}
        onSubmit={async (text, title) => {
          await api.updateEntry(e.id, text, title);
          setEditing(false);
          entry.reload();
        }}
      />
    );
  return (
    <section>
      <p className="muted">
        {e.date.replace("T", " ")} · {e.id} {!e.processed && <Badge tone="pending">processing…</Badge>}
      </p>
      {e.title && <h2>{e.title}</h2>}
      <div className="entry-text">{e.text}</div>
      <div className="toolbar">
        <button onClick={() => setEditing(true)}>Edit</button>
      </div>
      <h3>Fragments</h3>
      {e.fragments.length === 0 && <p className="muted">{e.processed ? "No fragments." : "Not processed yet."}</p>}
      {e.fragments.map((f) => (
        <FragmentCard key={f.id} fragment={f} onChanged={entry.reload} />
      ))}
    </section>
  );
}

export function EditorPage() {
  const navigate = useNavigate();
  return (
    <EntryForm
      submitLabel="Save entry"
      onSubmit={async (text, title) => {
        const { id } = await api.createEntry(text, title);
        navigate(`/entries/${encodeURIComponent(id)}`);
      }}
    />
  );
}

function EntryForm({
  initialText = "",
  initialTitle = "",
  submitLabel,
  onSubmit,
  onCancel,
}: {
  initialText?: string;
  initialTitle?: string;
  submitLabel: string;
  onSubmit: (text: string, title: string) => Promise<void>;
  onCancel?: () => void;
}) {
  const [text, setText] = useState(initialText);
  const [title, setTitle] = useState(initialTitle);
  const { busy, error, run } = useAction(true);
  const submit = () => run(() => onSubmit(text, title));

  return (
    <section className="editor">
      <input placeholder="Title (optional)" value={title} onChange={(e) => setTitle(e.target.value)} />
      <textarea
        autoFocus
        placeholder="What happened, what was decided, what is still open…"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && text.trim()) void submit();
        }}
        rows={16}
      />
      <div className="toolbar">
        <button className="primary" disabled={busy || !text.trim()} onClick={() => void submit()}>
          {busy ? "Saving…" : submitLabel}
        </button>
        {onCancel && <button onClick={onCancel}>Cancel</button>}
        <span className="muted">⌘/Ctrl + Enter to save</span>
      </div>
      {error && <p className="error">{error}</p>}
    </section>
  );
}
