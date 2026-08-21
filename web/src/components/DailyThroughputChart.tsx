import { Spinner } from "@nous-research/ui/ui/components/spinner";

import { ProjectPanel } from "@/components/ProjectHermesUi";
import type { DailyThroughputPoint } from "@/lib/project-throughput";

type ThroughputMetric = Exclude<keyof DailyThroughputPoint, "date">;

interface SeriesDefinition {
  key: ThroughputMetric;
  label: string;
  color: string;
}

const CHART_WIDTH = 700;
const CHART_HEIGHT = 176;
const PLOT_LEFT = 30;
const PLOT_RIGHT = 8;
const PLOT_TOP = 12;
const PLOT_BOTTOM = 32;

function formatDay(date: string): string {
  return new Intl.DateTimeFormat(undefined, {
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  }).format(new Date(`${date}T00:00:00Z`));
}

function ThroughputBars({
  data,
  title,
  series,
}: {
  data: readonly DailyThroughputPoint[];
  title: string;
  series: readonly [SeriesDefinition, SeriesDefinition];
}) {
  const plotWidth = CHART_WIDTH - PLOT_LEFT - PLOT_RIGHT;
  const plotHeight = CHART_HEIGHT - PLOT_TOP - PLOT_BOTTOM;
  const maxValue = Math.max(
    1,
    ...data.flatMap((point) => series.map(({ key }) => point[key])),
  );
  const groupWidth = plotWidth / Math.max(1, data.length);
  const barGap = 2;
  const barWidth = Math.min(13, Math.max(2, (groupWidth - 6) / 2));
  const gridValues = Array.from(new Set([0, Math.ceil(maxValue / 2), maxValue]));
  const totals = series.map(({ key }) =>
    data.reduce((sum, point) => sum + point[key], 0),
  );

  return (
    <section className="min-w-0 rounded-md border border-border bg-muted/10 px-3 pb-2 pt-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h4 className="text-[11px] font-semibold">{title}</h4>
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-[9px] text-muted-foreground">
          {series.map((item, index) => (
            <span key={item.key} className="inline-flex items-center gap-1">
              <span className="h-2 w-2 rounded-sm" style={{ backgroundColor: item.color }} />
              {item.label} · {totals[index]}
            </span>
          ))}
        </div>
      </div>
      <svg
        className="mt-2 h-auto w-full overflow-visible"
        viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
        role="img"
        aria-label={`${title} by UTC day`}
      >
        {gridValues.map((value) => {
          const y = PLOT_TOP + plotHeight - (value / maxValue) * plotHeight;
          return (
            <g key={value}>
              <line
                x1={PLOT_LEFT}
                x2={CHART_WIDTH - PLOT_RIGHT}
                y1={y}
                y2={y}
                className="stroke-border"
                strokeDasharray={value === 0 ? undefined : "3 4"}
              />
              <text
                x={PLOT_LEFT - 6}
                y={y + 3}
                textAnchor="end"
                className="fill-muted-foreground text-[9px]"
              >
                {value}
              </text>
            </g>
          );
        })}
        {data.map((point, index) => {
          const center = PLOT_LEFT + groupWidth * index + groupWidth / 2;
          const showLabel =
            data.length <= 7 || index % 2 === 0 || index === data.length - 1;
          return (
            <g key={point.date} aria-label={`${formatDay(point.date)}: ${series
              .map(({ key, label }) => `${label} ${point[key]}`)
              .join(", ")}`}>
              {series.map(({ key, label, color }, seriesIndex) => {
                const value = point[key];
                const height = (value / maxValue) * plotHeight;
                const x = center + (seriesIndex === 0 ? -barWidth - barGap / 2 : barGap / 2);
                return (
                  <rect
                    key={key}
                    x={x}
                    y={PLOT_TOP + plotHeight - height}
                    width={barWidth}
                    height={height}
                    rx={2}
                    fill={color}
                  >
                    <title>{`${formatDay(point.date)} · ${label}: ${value}`}</title>
                  </rect>
                );
              })}
              {showLabel ? (
                <text
                  x={center}
                  y={CHART_HEIGHT - 10}
                  textAnchor="middle"
                  className="fill-muted-foreground text-[9px]"
                >
                  {formatDay(point.date)}
                </text>
              ) : null}
            </g>
          );
        })}
      </svg>
    </section>
  );
}

export function DailyThroughputChart({
  data,
  loading,
  error,
}: {
  data: readonly DailyThroughputPoint[] | null;
  loading: boolean;
  error: string | null;
}) {
  return (
    <ProjectPanel
      title="Daily throughput · last 14 UTC days"
      subtitle="Issue intake and delivery milestones, zero-filled on quiet days"
      action={loading ? <Spinner /> : null}
      bodyClassName="p-4"
    >
      {data ? (
        <>
          <div className="grid gap-3 xl:grid-cols-2">
            <ThroughputBars
              data={data}
              title="Issue intake"
              series={[
                { key: "collected", label: "Collected", color: "#3b82f6" },
                { key: "selected", label: "Selected", color: "#14b8a6" },
              ]}
            />
            <ThroughputBars
              data={data}
              title="Delivery"
              series={[
                { key: "queued", label: "Queued", color: "#f59e0b" },
                { key: "resolved", label: "Resolved", color: "#16a34a" },
              ]}
            />
          </div>
          <p className="mt-3 text-[9px] leading-4 text-muted-foreground">
            Collected uses first discovery; Selected uses the accepted screening time;
            Queued uses Work creation; Resolved counts Work completed with status done.
          </p>
          {error ? (
            <p role="status" className="mt-2 text-[10px] text-amber-700">
              Latest chart refresh failed; showing the last successful snapshot. {error}
            </p>
          ) : null}
        </>
      ) : error ? (
        <p role="alert" className="py-12 text-center text-xs text-red-700">
          Daily throughput is unavailable. {error}
        </p>
      ) : (
        <div className="flex min-h-48 items-center justify-center"><Spinner /></div>
      )}
    </ProjectPanel>
  );
}

