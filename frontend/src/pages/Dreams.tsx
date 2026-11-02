import { api } from "../api";
import { FragmentCard, Loading } from "../components";
import { useLoad } from "../hooks";

export function DreamsPage() {
  const { data, error, loading, reload } = useLoad(() => api.dreams());
  return (
    <section>
      <h2>Dreams</h2>
      <p className="muted">Big ideas not tied to any current work.</p>
      <Loading error={error} loading={loading && !data} />
      {data?.length === 0 && <p>No dreams yet. Mark a fragment as a dream, or write one down.</p>}
      {data?.map((f) => (
        <FragmentCard key={f.id} fragment={f} onChanged={reload} />
      ))}
    </section>
  );
}
