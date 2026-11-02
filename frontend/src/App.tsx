import { lazy, Suspense } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { api } from "./api";
import { useInterval, useLoad } from "./hooks";
import { ActivePage } from "./pages/Active";
import { DreamsPage } from "./pages/Dreams";
import { EditorPage, EntriesPage, EntryPage } from "./pages/Entries";
import { EntityListPage, EntityPage } from "./pages/Entity";
import { ReviewPage } from "./pages/Review";
import { SearchPage } from "./pages/Search";
import { SweepPage } from "./pages/Sweep";

// The graph explorer pulls in Cytoscape; load it only when the page is opened.
const GraphPage = lazy(() => import("./pages/Graph").then((m) => ({ default: m.GraphPage })));

export function App() {
  const status = useLoad(() => api.status());
  useInterval(status.reload, 10_000, true);
  const s = status.data;

  return (
    <div className="layout">
      <nav className="sidebar">
        <h1>brain</h1>
        <NavLink to="/entries/new" className="button primary">
          New entry
        </NavLink>
        <NavLink to="/" end>
          Active
        </NavLink>
        <NavLink to="/search">Search &amp; ask</NavLink>
        <NavLink to="/review">
          Review {s && s.review > 0 && <span className="count">{s.review}</span>}
        </NavLink>
        <NavLink to="/entries">Entries</NavLink>
        <NavLink to="/threads">Threads</NavLink>
        <NavLink to="/people">People</NavLink>
        <NavLink to="/orgs">Orgs</NavLink>
        <NavLink to="/themes">Themes</NavLink>
        <NavLink to="/dreams">Dreams</NavLink>
        <NavLink to="/tidy">Tidy up</NavLink>
        <NavLink to="/graph">Graph</NavLink>
        <footer className="muted">
          {s && (
            <>
              <div>{s.entries} entries</div>
              {s.pending > 0 && <div>{s.pending} processing</div>}
              {s.failures > 0 && <div className="error">{s.failures} failed</div>}
              {!s.llm_configured && <div>no model endpoint</div>}
              {s.worker?.last_error && <div className="error" title={s.worker.last_error}>worker error</div>}
            </>
          )}
          {status.error && <div className="error">server unreachable</div>}
        </footer>
      </nav>
      <main>
        <Routes>
          <Route path="/" element={<ActivePage />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/review" element={<ReviewPage onChange={status.reload} />} />
          <Route path="/entries" element={<EntriesPage />} />
          <Route path="/entries/new" element={<EditorPage />} />
          <Route path="/entries/:id" element={<EntryPage />} />
          <Route path="/threads" element={<EntityListPage type="thread" title="Threads" />} />
          <Route path="/people" element={<EntityListPage type="person" title="People" />} />
          <Route path="/orgs" element={<EntityListPage type="org" title="Organizations" />} />
          <Route path="/themes" element={<EntityListPage type="theme" title="Themes" />} />
          <Route path="/e/:nodeId" element={<EntityPage />} />
          <Route path="/dreams" element={<DreamsPage />} />
          <Route path="/tidy" element={<SweepPage />} />
          <Route
            path="/graph"
            element={
              <Suspense fallback={<p className="muted">Loading…</p>}>
                <GraphPage />
              </Suspense>
            }
          />
          <Route path="*" element={<p>Not found.</p>} />
        </Routes>
      </main>
    </div>
  );
}
