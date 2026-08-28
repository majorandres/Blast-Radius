// Domain-level topology, not process-level.
//
// Jaeger already shows the two real processes truthfully. This shows the four
// logical failure domains, which is what attribution actually reasons about --
// and it is why `payment-gateway` and `order-datastore` appear here as nodes
// with no process behind them. Their health comes from the CLIENT spans that
// carry their domain, which is the same evidence the detector uses.
//
// The layout is fixed and meaningful (caller on the left, its three
// dependencies on the right), so nothing here is auto-arranged.
//
// The coordinate space is sized close to the rendered width on purpose. A small
// viewBox stretched to full width scales the type up with it, which made the
// labels overpower the graphic and pushed the metrics line out of its box.

const WIDTH = 1120;
const HEIGHT = 300;
const BOX_W = 250;
const BOX_H = 96;

const POSITIONS = {
  "ordering-app": { x: 190, y: 150 },
  "promo-provider": { x: 830, y: 56 },
  "payment-gateway": { x: 830, y: 150 },
  "order-datastore": { x: 830, y: 244 },
};

const KIND_LABEL = {
  process: "PROCESS",
  logical_dependency: "LOGICAL DEPENDENCY",
  datastore: "DATASTORE",
};

// Health is a status, so every node carries a word as well as a colour.
function health(node) {
  if (!node.span_count) return { color: "var(--muted)", label: "no traffic" };
  if (node.error_pct >= 5) return { color: "var(--status-critical)", label: "failing" };
  if (node.p95_ms != null && node.p95_ms >= 1000) return { color: "var(--status-warning)", label: "slow" };
  if (node.error_pct >= 1) return { color: "var(--status-warning)", label: "degraded" };
  return { color: "var(--status-good)", label: "healthy" };
}

export default function Topology({ topology }) {
  const nodes = topology?.nodes ?? [];
  const edges = topology?.edges ?? [];
  const byName = Object.fromEntries(nodes.map((n) => [n.name, n]));

  return (
    <div className="card">
      <h2>Failure domains</h2>
      <svg
        className="chart topology"
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label="Domain topology with per-domain health"
      >
        {edges.map((e) => {
          const from = POSITIONS[e.caller];
          const to = POSITIONS[e.callee];
          if (!from || !to) return null;
          return (
            <line
              key={`${e.caller}-${e.callee}`}
              className="edge"
              x1={from.x + BOX_W / 2}
              y1={from.y}
              x2={to.x - BOX_W / 2}
              y2={to.y}
            />
          );
        })}

        {Object.entries(POSITIONS).map(([name, pos]) => {
          const node = byName[name];
          if (!node) return null;
          const state = health(node);
          const metrics = [
            node.p95_ms != null ? `p95 ${node.p95_ms}ms` : null,
            node.error_pct ? `${node.error_pct}% err` : null,
          ].filter(Boolean).join("  ·  ");

          return (
            <g key={name}>
              <rect
                x={pos.x - BOX_W / 2}
                y={pos.y - BOX_H / 2}
                width={BOX_W}
                height={BOX_H}
                rx={9}
                fill="var(--surface-1)"
                stroke={state.color}
                strokeWidth={2}
              />
              <text className="node-name" x={pos.x} y={pos.y - 24} textAnchor="middle">
                {name}
              </text>
              <text className="kind" x={pos.x} y={pos.y - 7} textAnchor="middle">
                {KIND_LABEL[node.kind] ?? node.kind}
              </text>
              <text
                className="node-state"
                x={pos.x}
                y={pos.y + 14}
                textAnchor="middle"
                fill={state.color}
              >
                {state.label}
              </text>
              {/* Inside the box: at the previous offset this line fell below the
                  border and collided with the node on the row beneath. */}
              <text className="node-meta" x={pos.x} y={pos.y + 33} textAnchor="middle">
                {metrics || "—"}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
