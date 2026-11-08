import { api } from "../api";
import { FragmentCard, Loading } from "../components";
import { useLoad } from "../hooks";

export function SomedayPage() {
  const { data, error, loading, reload } = useLoad(() => api.someday());
  return (
    <section>
      <h2>Someday</h2>
      <p className="muted">Big ideas not tied to any current work.</p>
      <Loading error={error} loading={loading && !data} />
      {data?.length === 0 && <p>Nothing here yet. Mark a fragment as someday, or write one down.</p>}
      {data?.map((f) => (
        <FragmentCard key={f.id} fragment={f} onChanged={reload} />
      ))}
    </section>
  );
}
