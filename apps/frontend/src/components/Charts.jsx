import { useMemo, useState } from "react";

// Two charts, never one with two y-axes. Checkout success and p95 latency are
// different measures on different scales; overlaying them on a shared axis is
// the single most common charting mistake and it would also bury the point --
// under a fail-slow fault latency moves while availability holds, and that
// divergence is only visible when each has its own scale.

const PAD = { top: 18, right: 62, bottom: 24, left: 52 };
const WIDTH = 760;
const HEIGHT = 240;

function niceTicks(min, max, count = 4) {
  if (min === max) return [min];
  const raw = (max - min) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? mag * 10;
  const start = Math.ceil(min / step) * step;
  const ticks = [];
  for (let v = start; v <= max + step * 0.001; v += step) ticks.push(Number(v.toFixed(6)));
  return ticks;
}

function LineChart({ title, note, points, accessor, threshold, thresholdLabel, format, color, domain }) {
  const [hover, setHover] = useState(null);

  const series = useMemo(
    () => points.map((p, i) => ({ i, ts: p.ts, value: accessor(p) })).filter((p) => p.value != null),
    [points, accessor],
  );

  if (series.length < 2) {
    return (
      <div>
        <div className="chart-title">{title}</div>
        <div className="chart-note">{note}</div>
        <p className="empty">Not enough data yet.</p>
      </div>
    );
  }

  const values = series.map((p) => p.value);
  // The threshold is always in frame: a chart that crops its own SLO line
  // cannot show whether the line was crossed.
  let lo = domain?.[0] ?? Math.min(...values, threshold ?? Infinity);
  let hi = domain?.[1] ?? Math.max(...values, threshold ?? -Infinity);
  if (lo === hi) { lo -= 1; hi += 1; }
  const headroom = (hi - lo) * 0.12;
  lo = domain?.[0] ?? lo - headroom;
  hi = domain?.[1] ?? hi + headroom;

  const plotW = WIDTH - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const x = (i) => PAD.left + (i / (series.length - 1)) * plotW;
  const y = (v) => PAD.top + plotH - ((v - lo) / (hi - lo)) * plotH;

  const path = series.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
  const ticks = niceTicks(lo, hi);
  const last = series[series.length - 1];

  const onMove = (event) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const px = ((event.clientX - rect.left) / rect.width) * WIDTH;
    const idx = Math.round(((px - PAD.left) / plotW) * (series.length - 1));
    const clamped = Math.max(0, Math.min(series.length - 1, idx));
    setHover({ ...series[clamped], idx: clamped });
  };

  return (
    <div className="chart-wrap">
      <div className="chart-title">{title}</div>
      <div className="chart-note">{note}</div>
      <svg
        className="chart"
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label={`${title}. Latest ${format(last.value)}.`}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line className="gridline" x1={PAD.left} x2={PAD.left + plotW} y1={y(t)} y2={y(t)} />
            <text className="tick" x={PAD.left - 6} y={y(t) + 3} textAnchor="end">{format(t)}</text>
          </g>
        ))}

        <line className="axis" x1={PAD.left} x2={PAD.left + plotW} y1={PAD.top + plotH} y2={PAD.top + plotH} />

        {threshold != null && threshold >= lo && threshold <= hi && (
          <g>
            <line className="threshold" x1={PAD.left} x2={PAD.left + plotW} y1={y(threshold)} y2={y(threshold)} />
            {/* Above the line and inset from the axis. On the right it collided
                with the latest-value label whenever the series sat near the
                threshold -- exactly when both matter most -- and flush left it
                touched the topmost y-axis tick. */}
            <text className="threshold-label" x={PAD.left + 46} y={y(threshold) - 5}>
              SLO {thresholdLabel}
            </text>
          </g>
        )}

        <path className="line" d={path} stroke={color} />

        {/* Selective direct label: the latest value only, never one per point. */}
        <circle className="marker" cx={x(series.length - 1)} cy={y(last.value)} r={4} fill={color} />
        <text
          className="last-label"
          x={x(series.length - 1) + 7}
          y={y(last.value) + 4}
          fill={color}
        >
          {format(last.value)}
        </text>

        {hover && (
          <g>
            <line className="crosshair" x1={x(hover.idx)} x2={x(hover.idx)} y1={PAD.top} y2={PAD.top + plotH} />
            <circle className="marker" cx={x(hover.idx)} cy={y(hover.value)} r={4.5} fill={color} />
          </g>
        )}
      </svg>

      {hover && (
        <div
          className="tooltip"
          style={{
            left: `${(x(hover.idx) / WIDTH) * 100}%`,
            top: 0,
            transform: `translate(${hover.idx > series.length / 2 ? "-108%" : "8%"}, 0)`,
          }}
        >
          <strong>{format(hover.value)}</strong>
          <span style={{ color: "var(--muted)", marginLeft: 6 }}>
            {new Date(hover.ts).toLocaleTimeString()}
          </span>
        </div>
      )}
    </div>
  );
}

export default function Charts({ series }) {
  const points = series?.points ?? [];
  return (
    <div className="card">
      <h2>Last 15 minutes</h2>
      <div className="grid-2">
        <LineChart
          title="Checkout success"
          note="SLO: at or above 98%"
          points={points}
          accessor={(p) => p.checkout_success_pct}
          threshold={98}
          thresholdLabel="98%"
          format={(v) => `${v.toFixed(0)}%`}
          color="var(--series-1)"
        />
        <LineChart
          title="Checkout latency, 95th percentile"
          note="SLO: at or below 1000ms"
          points={points}
          accessor={(p) => p.p95_latency_ms}
          threshold={1000}
          thresholdLabel="1000ms"
          format={(v) => `${Math.round(v)}ms`}
          color="var(--series-2)"
        />
      </div>
    </div>
  );
}
