import { CheckCircle2, ExternalLink, GitPullRequest } from "lucide-react";

import { ProjectPanel } from "@/components/ProjectHermesUi";
import {
  COMPLETED_SOLUTIONS,
  type CompletedSolution,
} from "@/lib/completed-solutions";
import { cn } from "@/lib/utils";

const UPSTREAM_TONES = {
  open: "border-blue-200 bg-blue-50 text-blue-700",
  merged: "border-violet-200 bg-violet-50 text-violet-700",
};

function SolutionStatus() {
  return (
    <span
      data-solution-status="done"
      className="project-data inline-flex items-center gap-1 rounded-full border border-green-200 bg-green-50 px-2 py-0.5 text-[9px] font-semibold text-green-700"
    >
      <CheckCircle2 className="h-3 w-3" />
      DONE
    </span>
  );
}

function UpstreamStatus({ solution }: { solution: CompletedSolution }) {
  return (
    <span
      className={cn(
        "project-data inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[9px] font-semibold uppercase",
        UPSTREAM_TONES[solution.upstreamState],
      )}
    >
      <GitPullRequest className="h-3 w-3" />
      PR {solution.upstreamState}
    </span>
  );
}

function PullRequestLink({ solution }: { solution: CompletedSolution }) {
  return (
    <a
      href={solution.pullRequestUrl}
      target="_blank"
      rel="noreferrer"
      className="project-id inline-flex items-center gap-1 font-semibold text-blue-600 hover:underline"
    >
      #{solution.pullRequestNumber}
      <ExternalLink className="h-3 w-3" />
    </a>
  );
}

export function CompletedSolutionsTable() {
  return (
    <ProjectPanel
      title={`Completed solutions · ${COMPLETED_SOLUTIONS.length}`}
      subtitle="Delivered fixes with their current upstream pull-request state"
    >
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-left text-[11px]">
          <thead className="border-b border-border bg-muted/30 text-[9px] uppercase tracking-[0.08em] text-muted-foreground">
            <tr>
              <th className="px-4 py-2.5 font-semibold">Repository / PR</th>
              <th className="px-3 py-2.5 font-semibold">Completed solution</th>
              <th className="px-3 py-2.5 font-semibold">Resolution</th>
              <th className="px-4 py-2.5 font-semibold">Upstream</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {COMPLETED_SOLUTIONS.map((solution) => (
              <tr
                key={solution.id}
                data-completed-solution={solution.id}
                className="transition hover:bg-muted/30"
              >
                <td className="px-4 py-3 align-top">
                  <p className="font-semibold">{solution.repository}</p>
                  <p className="mt-1 text-[10px]">
                    <PullRequestLink solution={solution} />
                  </p>
                </td>
                <td className="max-w-[38rem] px-3 py-3 align-top">
                  <p className="font-semibold">{solution.title}</p>
                  <p className="mt-1 leading-5 text-muted-foreground">
                    {solution.summary}
                  </p>
                </td>
                <td className="px-3 py-3 align-top">
                  <SolutionStatus />
                </td>
                <td className="px-4 py-3 align-top">
                  <UpstreamStatus solution={solution} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </ProjectPanel>
  );
}

export function CompletedSolutionsCards() {
  return (
    <ProjectPanel
      title={`Completed solutions · ${COMPLETED_SOLUTIONS.length}`}
      subtitle="Finished engineering outcomes remain separate from the live Work queue"
      bodyClassName="grid gap-3 p-4 md:grid-cols-2 2xl:grid-cols-4"
    >
      {COMPLETED_SOLUTIONS.map((solution) => (
        <article
          key={solution.id}
          data-completed-solution={solution.id}
          className="rounded-md border border-border bg-slate-50/60 p-3"
        >
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="truncate text-[10px] font-semibold text-muted-foreground">
                {solution.repository}
              </p>
              <p className="mt-1 text-[10px]">
                <PullRequestLink solution={solution} />
              </p>
            </div>
            <SolutionStatus />
          </div>
          <h3 className="mt-3 text-xs font-semibold leading-5">
            {solution.title}
          </h3>
          <p className="mt-1 line-clamp-3 text-[10px] leading-4 text-muted-foreground">
            {solution.summary}
          </p>
          <div className="mt-3">
            <UpstreamStatus solution={solution} />
          </div>
        </article>
      ))}
    </ProjectPanel>
  );
}
