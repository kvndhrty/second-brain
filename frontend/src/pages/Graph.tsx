import cytoscape from "cytoscape";
import { useEffect, useRef } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, nodePath, type GraphData } from "../api";
import { Loading } from "../components";
import { useLoad } from "../hooks";

const COLORS: Record<string, string> = {
  thread: "#2f6fde",
  person: "#d9822b",
  org: "#7a5af5",
  theme: "#1f9d6b",
};

// Two nodes are joined when fragments link to both. A technique theme joined
// to two threads means the same approach was used in both.
export function GraphPage() {
  const [params] = useSearchParams();
  const center = params.get("center") ?? undefined;
  const { data, error, loading } = useLoad(() => api.graph(center), [center]);
  return (
    <section className="graph-page">
      <h2>Graph</h2>
      <p className="muted">
        Threads, people, orgs, and themes; lines join things that appear in the same fragments. Click a node to
        open it.
        {center && (
          <>
            {" "}
            Showing two steps around <strong>{center}</strong>. <Link to="/graph">Show everything</Link>
          </>
        )}
      </p>
      <p className="legend">
        {Object.entries(COLORS).map(([type, color]) => (
          <span key={type}>
            <i style={{ background: color }} /> {type}
          </span>
        ))}
      </p>
      <Loading error={error} loading={loading && !data} />
      {data && data.truncated && <p className="muted">Showing the most connected {data.nodes.length} nodes.</p>}
      {data && <GraphCanvas data={data} center={center} />}
    </section>
  );
}

function GraphCanvas({ data, center }: { data: GraphData; center?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  useEffect(() => {
    if (!ref.current) return;
    const maxW = Math.max(1, ...data.nodes.map((n) => n.weight));
    const cy = cytoscape({
      container: ref.current,
      elements: [
        ...data.nodes.map((n) => ({
          data: { id: n.id, label: n.label, color: COLORS[n.type] ?? "#888", size: 14 + 36 * Math.sqrt(n.weight / maxW) },
          classes: n.id === center ? "center" : "",
        })),
        ...data.edges.map((e) => ({
          data: { id: `${e.source}|${e.target}|${e.type}`, source: e.source, target: e.target, w: Math.min(8, 1 + e.weight) },
          classes: e.type,
        })),
      ],
      style: [
        {
          selector: "node",
          style: {
            "background-color": "data(color)",
            label: "data(label)",
            width: "data(size)",
            height: "data(size)",
            "font-size": 10,
            color: "#666",
            "text-valign": "bottom",
            "text-margin-y": 4,
          },
        },
        { selector: "node.center", style: { "border-width": 3, "border-color": "#000" } },
        { selector: "edge", style: { width: "data(w)", "line-color": "#bbb", opacity: 0.6, "curve-style": "haystack" } },
        { selector: "edge.member_of, edge.part_of", style: { "line-style": "dashed" } },
      ],
      layout: { name: "cose", animate: false, nodeRepulsion: () => 9000, idealEdgeLength: () => 90, padding: 40 },
      // Without a cap, a graph with a few nodes is zoomed until they fill the canvas.
      maxZoom: 1.5,
      minZoom: 0.2,
    });
    cy.on("tap", "node", (evt) => navigate(nodePath(evt.target.id())));
    return () => cy.destroy();
  }, [data, center, navigate]);
  return <div ref={ref} className="graph" data-testid="graph" />;
}
