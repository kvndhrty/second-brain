import { NavLink, Route, Routes } from "react-router-dom";
import { api } from "./api";
import { useInterval, useLoad } from "./hooks";
import { HomePage } from "./pages/Home";
import { LogPage } from "./pages/Log";
import { NotePage, NotesPage } from "./pages/Notes";
import { SearchPage } from "./pages/Search";
import { EntityPage, WikiPage } from "./pages/Wiki";

export function App() {
  const status = useLoad(() => api.status());
  useInterval(status.reload, 10_000, true);
  const s = status.data;

  return (
    <div className="layout">
      <nav className="sidebar">
        <h1>brain</h1>
        <NavLink to="/" end className="button primary">
          Write a note
        </NavLink>
        <NavLink to="/wiki">Wiki</NavLink>
        <NavLink to="/search">Search &amp; ask</NavLink>
        <NavLink to="/notes">Notes</NavLink>
        <NavLink to="/log">Change log</NavLink>
        <footer className="muted">
          {s && (
            <>
              <div>
                {s.notes} notes · {s.pages} pages
              </div>
              {s.pending > 0 && <div>{s.pending} processing</div>}
              {s.failures > 0 && <div className="error">{s.failures} failed</div>}
              {!s.llm_configured && <div>no model endpoint</div>}
              {s.worker?.last_error && (
                <div className="error" title={s.worker.last_error}>
                  worker error
                </div>
              )}
            </>
          )}
          {status.error && <div className="error">server unreachable</div>}
        </footer>
      </nav>
      <main>
        <Routes>
          <Route path="/" element={<HomePage onSaved={status.reload} />} />
          <Route path="/wiki" element={<WikiPage />} />
          <Route path="/p/:id" element={<EntityPage />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/notes" element={<NotesPage />} />
          <Route path="/notes/:id" element={<NotePage />} />
          <Route path="/log" element={<LogPage />} />
          <Route path="*" element={<p>Not found.</p>} />
        </Routes>
      </main>
    </div>
  );
}
