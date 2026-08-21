import type { ComponentType, ReactNode } from "react";
import { cn } from "@/lib/utils";
import type {
  PollingRepositoryStat,
  ProjectWorkStatus,
  WorkEnvironmentStatus,
} from "@/lib/api";

export function formatProjectTime(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

export function formatProjectInterval(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${Math.round((seconds / 3600) * 10) / 10}h`;
}

const WORK_TONES: Record<ProjectWorkStatus, string> = {
  queued: "border-slate-200 bg-slate-100 text-slate-600",
  planning: "border-violet-200 bg-violet-50 text-violet-700",
  running: "border-blue-200 bg-blue-50 text-blue-700",
  review: "border-amber-200 bg-amber-50 text-amber-700",
  done: "border-green-200 bg-green-50 text-green-700",
  blocked: "border-orange-200 bg-orange-50 text-orange-700",
  failed: "border-red-200 bg-red-50 text-red-700",
};

const ENVIRONMENT_TONES: Record<WorkEnvironmentStatus, string> = {
  pending: "border-slate-200 bg-slate-100 text-slate-600",
  verified: "border-green-200 bg-green-50 text-green-700",
  failed: "border-red-200 bg-red-50 text-red-700",
};

interface StatusBadgeProps {
  status: ProjectWorkStatus;
}

export function WorkStatusBadge({ status }: StatusBadgeProps) {
  return (
    <span
      className={cn(
        "project-data inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase",
        WORK_TONES[status],
      )}
    >
      {status}
    </span>
  );
}

export function EnvironmentBadge({ status }: { status: WorkEnvironmentStatus }) {
  return (
    <span
      className={cn(
        "project-data inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase",
        ENVIRONMENT_TONES[status],
      )}
    >
      env {status}
    </span>
  );
}

interface ProjectPanelProps {
  title?: string;
  subtitle?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}

export function ProjectPanel({
  title,
  subtitle,
  action,
  children,
  className,
  bodyClassName,
}: ProjectPanelProps) {
  return (
    <section
      className={cn(
        "overflow-hidden rounded-lg border border-border bg-card text-card-foreground shadow-[0_2px_8px_rgba(0,0,0,0.06)]",
        className,
      )}
    >
      {title || subtitle || action ? (
        <header className="flex min-h-14 items-center justify-between gap-4 border-b border-border px-4 py-3">
          <div className="min-w-0">
            {title ? (
              <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em]">
                {title}
              </h3>
            ) : null}
            {subtitle ? (
              <p className="mt-1 truncate text-[11px] text-muted-foreground">
                {subtitle}
              </p>
            ) : null}
          </div>
          {action}
        </header>
      ) : null}
      <div className={bodyClassName}>{children}</div>
    </section>
  );
}

interface ProjectMetricProps {
  label: string;
  value: number | string;
  detail: string;
  icon: ComponentType<{ className?: string }>;
  tone?: "blue" | "teal" | "violet" | "amber" | "rose" | "slate";
}

const METRIC_TONES = {
  blue: "bg-blue-50 text-blue-600",
  teal: "bg-teal-50 text-teal-600",
  violet: "bg-violet-50 text-violet-600",
  amber: "bg-amber-50 text-amber-600",
  rose: "bg-rose-50 text-rose-600",
  slate: "bg-slate-100 text-slate-600",
};

export function ProjectMetric({
  label,
  value,
  detail,
  icon: Icon,
  tone = "blue",
}: ProjectMetricProps) {
  return (
    <article className="group rounded-lg border border-border bg-card px-4 py-3.5 transition hover:border-blue-500 hover:shadow-[0_2px_8px_rgba(0,0,0,0.06)]">
      <div className="flex items-center justify-between gap-3">
        <p className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          {label}
        </p>
        <span className={cn("rounded-md p-1.5", METRIC_TONES[tone])}>
          <Icon className="h-3.5 w-3.5" />
        </span>
      </div>
      <p className="project-data mt-2 text-2xl font-bold leading-none">{value}</p>
      <p className="mt-2 truncate text-[10px] text-muted-foreground">{detail}</p>
    </article>
  );
}

interface RepositorySelectProps {
  repositories: PollingRepositoryStat[];
  value: number | null;
  onChange: (value: number | null) => void;
  className?: string;
}

export function RepositorySelect({
  repositories,
  value,
  onChange,
  className,
}: RepositorySelectProps) {
  return (
    <label className={cn("flex min-w-0 items-center gap-2", className)}>
      <span className="shrink-0 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
        Repository
      </span>
      <select
        className="project-data min-w-0 rounded-md border border-border bg-card px-2.5 py-1.5 text-[11px] text-foreground outline-none transition focus:border-blue-500"
        value={value ?? ""}
        onChange={(event) =>
          onChange(event.target.value ? Number(event.target.value) : null)
        }
      >
        <option value="">All repositories</option>
        {repositories.map((repository) => (
          <option key={repository.repository_id} value={repository.repository_id}>
            {repository.repository} · {repository.repository_id}
          </option>
        ))}
      </select>
    </label>
  );
}
