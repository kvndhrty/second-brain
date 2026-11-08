import { useState, type ReactNode } from "react";
import { Link as RouterLink } from "react-router-dom";
import { api, nodePath, type Claim, type Fragment, type FragmentPatch, type Link, type Source } from "./api";

export function Loading({ error, loading }: { error?: string; loading: boolean }) {
  if (error) return <p className="error">{error}</p>;
  if (loading) return <p className="muted">Loading…</p>;
  return null;
}

const KIND_LABEL: Record<string, string> = {
  observation: "observation",
  decision: "decision",
  question: "question",
  idea: "idea",
  claim: "claim",
  action_item: "action",
};

const STATUS_LABEL: Record<Fragment["status"], string> = {
  confirmed: "confirmed",
  speculative: "speculative",
  overcome_by_events: "overcome by events",
};

export function Badge({ children, tone }: { children: ReactNode; tone?: string }) {
  return <span className={`badge ${tone ?? ""}`}>{children}</span>;
}

export function EntryLink({ id, children }: { id: string; children?: ReactNode }) {
  return <RouterLink to={`/entries/${encodeURIComponent(id)}`}>{children ?? id}</RouterLink>;
}

export function NodeChip({ link }: { link: Link }) {
  return (
    <RouterLink className={`chip ${link.type}`} to={nodePath(link.id)}>
      {link.label}
    </RouterLink>
  );
}

// One fragment, with its badges and the actions that change its status.
// Changes are saved to the entry's decisions file, so they survive re-extraction.
export function FragmentCard({
  fragment,
  links = [],
  onChanged,
}: {
  fragment: Fragment;
  links?: Link[];
  onChanged?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const f = fragment;

  async function change(patch: FragmentPatch) {
    setBusy(true);
    setError(undefined);
    try {
      await api.patchFragment(f.id, patch);
      onChanged?.();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function markObe() {
    const reason = window.prompt("Why is this overcome by events? (optional)");
    if (reason === null) return;
    void change({ status: "overcome_by_events", obe_reason: reason || null });
  }

  return (
    <article className={`fragment ${f.status} ${f.state === "closed" ? "closed" : ""}`} data-testid="fragment">
      <p className="text">{f.text}</p>
      <div className="meta">
        <Badge tone={f.kind}>{KIND_LABEL[f.kind] ?? f.kind}</Badge>
        {f.status !== "confirmed" && <Badge tone={f.status}>{STATUS_LABEL[f.status]}</Badge>}
        {f.state && <Badge tone={f.state}>{f.state}</Badge>}
        {f.someday && <Badge tone="someday">someday</Badge>}
        {f.obe_reason && <span className="muted">({f.obe_reason})</span>}
        <EntryLink id={f.entry_id}>{f.date}</EntryLink>
        {links.map((l) => (
          <NodeChip key={l.id} link={l} />
        ))}
      </div>
      <div className="actions">
        <select
          aria-label="status"
          value={f.status}
          disabled={busy}
          onChange={(e) => {
            const status = e.target.value as Fragment["status"];
            if (status === "overcome_by_events") markObe();
            else void change({ status });
          }}
        >
          <option value="confirmed">confirmed</option>
          <option value="speculative">speculative</option>
          <option value="overcome_by_events">overcome by events</option>
        </select>
        {f.state === "open" && (
          <button disabled={busy} onClick={() => void change({ state: "closed" })}>
            Close
          </button>
        )}
        {f.state === "closed" && (
          <button disabled={busy} onClick={() => void change({ state: "open" })}>
            Reopen
          </button>
        )}
        <button disabled={busy} onClick={() => void change({ someday: !f.someday })}>
          {f.someday ? "Not someday" : "Someday"}
        </button>
        {error && <span className="error">{error}</span>}
      </div>
    </article>
  );
}

// Numbers sources in the order they are first cited, like footnotes.
export function citationOrder(claims: Claim[]): string[] {
  const order: string[] = [];
  for (const c of claims) for (const id of c.citations) if (!order.includes(id)) order.push(id);
  return order;
}

export function ClaimList({ claims, order }: { claims: Claim[]; order: string[] }) {
  return (
    <ul className="claims">
      {claims.map((c, i) => (
        <li key={i}>
          {c.text}{" "}
          {c.citations.map((id) => (
            <sup key={id}>
              <EntryLink id={id}>[{order.indexOf(id) + 1}]</EntryLink>
            </sup>
          ))}
        </li>
      ))}
    </ul>
  );
}

export function SourceList({ order, sources }: { order: string[]; sources: Source[] }) {
  if (order.length === 0) return null;
  const byId = new Map(sources.map((s) => [s.entry_id, s]));
  return (
    <ol className="sources">
      {order.map((id) => {
        const s = byId.get(id);
        return (
          <li key={id}>
            <EntryLink id={id}>{s ? `${s.date} · ${s.label}` : id}</EntryLink>
          </li>
        );
      })}
    </ol>
  );
}

// Thread summaries are short markdown with [entry-id] citations. This renders
// paragraphs, "- " bullets, **bold**, and turns citations into entry links.
export function SummaryText({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let bullets: string[] = [];
  const flush = () => {
    if (bullets.length) {
      blocks.push(
        <ul key={blocks.length}>
          {bullets.map((b, i) => (
            <li key={i}>{inline(b)}</li>
          ))}
        </ul>,
      );
      bullets = [];
    }
  };
  for (const line of text.split("\n")) {
    const t = line.trim();
    if (t.startsWith("- ") || t.startsWith("* ")) bullets.push(t.slice(2));
    else {
      flush();
      if (t) blocks.push(<p key={blocks.length}>{inline(t.replace(/^#+\s*/, ""))}</p>);
    }
  }
  flush();
  return <div className="summary">{blocks}</div>;
}

function inline(text: string): ReactNode[] {
  return text.split(/(\[e-[^\]\s]+\]|\*\*[^*]+\*\*)/g).map((part, i) => {
    if (/^\[e-[^\]\s]+\]$/.test(part)) {
      const id = part.slice(1, -1);
      return (
        <sup key={i}>
          <EntryLink id={id}>[{id.slice(2, 10)}]</EntryLink>
        </sup>
      );
    }
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={i}>{part.slice(2, -2)}</strong>;
    return part;
  });
}
