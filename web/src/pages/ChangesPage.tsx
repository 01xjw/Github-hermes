import {
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useSearchParams } from "react-router";
import {
  ArrowLeft,
  CheckCircle2,
  CircleDot,
  FileCode2,
  GitCommitHorizontal,
  GitCompare,
  LockKeyhole,
  RefreshCw,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Markdown } from "@/components/Markdown";
import { usePageHeader } from "@/contexts/usePageHeader";
import { api } from "@/lib/api";
import type {
  InternalPullRequestCandidate,
  InternalPullRequestCandidateFile,
  InternalPullRequestCandidateSummary,
  InternalPullRequestCheck,
} from "@/lib/api";
import {
  parseUnifiedDiff,
  unifiedLineKind,
  type DiffCell,
} from "@/lib/unified-diff";

type TabId =
  | "conversation"
  | "commits"
  | "files"
  | "checks"
  | "reviews"
  | "comparison"
  | "accounting";

const TABS: Array<{ id: TabId; label: string }> = [
  { id: "conversation", label: "Conversation" },
  { id: "commits", label: "Commits" },
  { id: "files", label: "Files changed" },
  { id: "checks", label: "Checks" },
  { id: "reviews", label: "Reviews" },
  { id: "comparison", label: "Comparison" },
  { id: "accounting", label: "Accounting" },
];

function statusLabel(status: string): string {
  return (
    {
      "?": "Added",
      A: "Added",
      C: "Copied",
      D: "Deleted",
      M: "Modified",
      R: "Renamed",
      T: "Type changed",
      U: "Conflicted",
    }[status] || status
  );
}

function lockLabel(candidate: {
  lock_state: InternalPullRequestCandidate["lock_state"];
}): string {
  return (
    {
      draft: "Draft",
      approval_pending: "Approval pending",
      immutable_approved: "Approved and locked",
    }[candidate.lock_state]
  );
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function formatDuration(milliseconds: number): string {
  if (milliseconds < 1_000) return `${milliseconds} ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1_000).toFixed(1)} s`;
  return `${(milliseconds / 60_000).toFixed(1)} min`;
}

function Metric({
  label,
  value,
  detail,
}: {
  label: string;
  value: ReactNode;
  detail: string;
}) {
  return (
    <article className="rounded-lg border border-[#e2e8f0] border-l-4 border-l-[#3b82f6] bg-white px-4 py-3 shadow-[0_2px_8px_rgba(0,0,0,0.06)]">
      <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[#64748b]">
        {label}
      </p>
      <div className="mt-1 text-xl font-semibold text-[#1e293b]">{value}</div>
      <p className="mt-1 truncate text-[11px] text-[#94a3b8]" title={detail}>
        {detail}
      </p>
    </article>
  );
}

function FileList({
  files,
  selectedPath,
  onSelect,
}: {
  files: InternalPullRequestCandidateFile[];
  selectedPath: string | null;
  onSelect: (file: InternalPullRequestCandidateFile) => void;
}) {
  if (!files.length) {
    return (
      <div className="flex min-h-64 items-center justify-center px-5 text-center text-sm text-[#64748b]">
        No production files are part of this candidate.
      </div>
    );
  }

  return (
    <ul className="max-h-[42rem] overflow-y-auto">
      {files.map((file) => (
        <li key={file.path}>
          <button
            type="button"
            onClick={() => onSelect(file)}
            className={`flex w-full items-start gap-3 border-b border-[#e2e8f0] px-3 py-3 text-left transition ${
              selectedPath === file.path
                ? "bg-[#eff6ff]"
                : "bg-white hover:bg-[#f8fafc]"
            }`}
          >
            <FileCode2
              className={`mt-0.5 h-4 w-4 shrink-0 ${
                selectedPath === file.path
                  ? "text-[#3b82f6]"
                  : "text-[#64748b]"
              }`}
            />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-xs font-semibold text-[#1e293b]">
                {file.path.split("/").at(-1)}
              </span>
              <span className="mt-0.5 block truncate text-[11px] text-[#94a3b8]">
                {file.path}
              </span>
              <span className="mt-1.5 flex items-center gap-2 text-[10px]">
                <span className="rounded-full bg-slate-100 px-1.5 py-0.5 font-semibold text-slate-600">
                  {statusLabel(file.status)}
                </span>
                <span className="font-mono font-semibold text-emerald-700">
                  +{file.added}
                </span>
                <span className="font-mono font-semibold text-red-700">
                  -{file.removed}
                </span>
              </span>
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

function DiffCellView({ cell }: { cell: DiffCell | null }) {
  const color =
    cell?.kind === "addition"
      ? "bg-emerald-50 text-emerald-950"
      : cell?.kind === "deletion"
        ? "bg-red-50 text-red-950"
        : "bg-white text-slate-700";
  const numberColor =
    cell?.kind === "addition"
      ? "bg-emerald-100/70 text-emerald-700"
      : cell?.kind === "deletion"
        ? "bg-red-100/70 text-red-700"
        : "bg-slate-50 text-slate-500";

  return (
    <div className={`grid min-w-0 grid-cols-[3.25rem_minmax(0,1fr)] ${color}`}>
      <span
        className={`select-none border-r border-slate-200 px-2 py-0.5 text-right font-mono text-[11px] ${numberColor}`}
      >
        {cell?.lineNumber ?? ""}
      </span>
      <pre className="overflow-visible px-2 py-0.5 font-mono text-xs leading-5">
        {cell?.text ?? " "}
      </pre>
    </div>
  );
}

function SplitDiff({ diff }: { diff: string }) {
  const document = useMemo(() => parseUnifiedDiff(diff), [diff]);

  if (!document.hunks.length) {
    return (
      <div className="flex min-h-72 items-center justify-center p-6 text-center text-sm text-[#64748b]">
        This file has no text diff.
      </div>
    );
  }

  return (
    <div className="min-w-[60rem] font-mono">
      <div className="grid grid-cols-2 border-b border-[#e2e8f0] bg-[#f8fafc] text-[11px] font-semibold text-[#64748b]">
        <div className="border-r border-[#e2e8f0] px-3 py-2">Original</div>
        <div className="px-3 py-2">Candidate</div>
      </div>
      {document.hunks.map((hunk, hunkIndex) => (
        <section key={`${hunk.header}:${hunkIndex}`}>
          <div className="border-y border-blue-100 bg-blue-50 px-3 py-1.5 text-[11px] text-blue-700">
            {hunk.header}
          </div>
          {hunk.rows.map((row, rowIndex) =>
            row.kind === "meta" ? (
              <div
                className="bg-slate-50 px-3 py-1 text-[11px] italic text-slate-500"
                key={`${hunkIndex}:meta:${rowIndex}`}
              >
                {row.text || " "}
              </div>
            ) : (
              <div
                className="grid grid-cols-2 border-b border-slate-100"
                key={`${hunkIndex}:content:${rowIndex}`}
              >
                <div className="min-w-0 overflow-visible border-r border-[#e2e8f0]">
                  <DiffCellView cell={row.left} />
                </div>
                <div className="min-w-0 overflow-visible">
                  <DiffCellView cell={row.right} />
                </div>
              </div>
            ),
          )}
        </section>
      ))}
    </div>
  );
}

function UnifiedDiff({ diff }: { diff: string }) {
  if (!diff.trim()) {
    return (
      <div className="flex min-h-72 items-center justify-center p-6 text-center text-sm text-[#64748b]">
        This file has no text diff.
      </div>
    );
  }

  const colors = {
    addition: "bg-emerald-50 text-emerald-950",
    deletion: "bg-red-50 text-red-950",
    context: "bg-white text-slate-700",
    header: "bg-slate-100 text-slate-600 font-semibold",
    hunk: "bg-blue-50 text-blue-700",
    meta: "bg-white text-slate-500",
  };

  return (
    <div className="min-w-max py-1 font-mono text-xs">
      {diff.replaceAll("\r\n", "\n").split("\n").map((line, index) => (
        <pre
          className={`min-h-5 px-3 leading-5 ${colors[unifiedLineKind(line)]}`}
          key={`${index}:${line.slice(0, 32)}`}
        >
          {line || " "}
        </pre>
      ))}
    </div>
  );
}

function CandidateList({
  candidates,
  loading,
  onSelect,
}: {
  candidates: InternalPullRequestCandidateSummary[];
  loading: boolean;
  onSelect: (candidateId: string) => void;
}) {
  if (loading && !candidates.length) {
    return (
      <div className="flex min-h-72 items-center justify-center">
        <Spinner />
      </div>
    );
  }
  if (!candidates.length) {
    return (
      <div className="flex min-h-72 items-center justify-center rounded-lg border border-[#e2e8f0] bg-white p-6 text-sm text-[#64748b]">
        No internal pull request candidates have been persisted.
      </div>
    );
  }

  return (
    <section className="overflow-hidden rounded-lg border border-[#e2e8f0] bg-white shadow-[0_2px_8px_rgba(0,0,0,0.06)]">
      <header className="border-b border-[#e2e8f0] bg-[#f8fafc] px-4 py-3">
        <p className="text-xs font-semibold text-[#1e293b]">
          Internal pull request candidates
        </p>
      </header>
      <ul>
        {candidates.map((candidate) => (
          <li className="border-b border-[#e2e8f0] last:border-b-0" key={candidate.candidate_id}>
            <button
              type="button"
              onClick={() => onSelect(candidate.candidate_id)}
              className="grid w-full gap-3 px-4 py-4 text-left transition hover:bg-[#f8fafc] md:grid-cols-[minmax(0,1fr)_12rem_9rem]"
            >
              <span className="min-w-0">
                <span className="flex items-center gap-2">
                  <GitCompare className="h-4 w-4 shrink-0 text-[#3b82f6]" />
                  <span className="truncate text-sm font-semibold text-[#1e293b]">
                    {candidate.title}
                  </span>
                </span>
                <span className="mt-1 block text-xs text-[#64748b]">
                  {candidate.repository} · {candidate.head_ref} → {candidate.base_ref}
                </span>
                <span className="mt-1 block text-[11px] text-[#94a3b8]">
                  Updated {formatDate(candidate.updated_at)}
                </span>
              </span>
              <span className="text-xs text-[#64748b]">
                {candidate.commit_count} commit{candidate.commit_count === 1 ? "" : "s"} ·{" "}
                {candidate.file_count} file{candidate.file_count === 1 ? "" : "s"}
                <span className="mt-1 block">
                  {candidate.checks_completed}/{candidate.checks_total} checks ·{" "}
                  {candidate.approvals}/{candidate.review_count} approvals
                </span>
              </span>
              <span
                className={`self-start justify-self-start rounded-full border px-2 py-0.5 text-[11px] font-semibold ${
                  candidate.lock_state === "immutable_approved"
                    ? "border-[#16a34a] text-[#16a34a]"
                    : "border-amber-500 text-amber-700"
                }`}
              >
                {lockLabel(candidate)}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function CheckIcon({ check }: { check: InternalPullRequestCheck }) {
  if (check.status !== "completed") {
    return <CircleDot className="h-5 w-5 text-amber-600" />;
  }
  if (check.conclusion === "success") {
    return <CheckCircle2 className="h-5 w-5 text-emerald-600" />;
  }
  return <XCircle className="h-5 w-5 text-red-600" />;
}

export default function ChangesPage() {
  const { setEnd } = usePageHeader();
  const [searchParams, setSearchParams] = useSearchParams();
  const candidateId = searchParams.get("candidate");
  const [candidates, setCandidates] = useState<InternalPullRequestCandidateSummary[]>([]);
  const [candidate, setCandidate] = useState<InternalPullRequestCandidate | null>(null);
  const [activeTab, setActiveTab] = useState<TabId>("conversation");
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [diff, setDiff] = useState("");
  const [diffView, setDiffView] = useState<"split" | "unified">("split");
  const [loading, setLoading] = useState(true);
  const [diffLoading, setDiffLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const detailRequest = candidateId
      ? api.getInternalPullRequestCandidate(candidateId)
      : Promise.resolve(null);
    Promise.all([
      api.listInternalPullRequestCandidates(),
      detailRequest,
    ])
      .then(([list, detail]) => {
        if (cancelled) return;
        setCandidates(list.candidates);
        setCandidate(detail);
        setActiveTab("conversation");
        setSelectedPath(detail?.files[0]?.path ?? null);
        setDiff("");
      })
      .catch((reason) => {
        if (!cancelled) {
          setCandidate(null);
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [candidateId, refreshToken]);

  const selectedFile = useMemo(
    () => candidate?.files.find((file) => file.path === selectedPath) ?? null,
    [candidate, selectedPath],
  );

  useEffect(() => {
    if (activeTab !== "files" || !candidate || !selectedFile) {
      setDiff("");
      return;
    }
    let cancelled = false;
    setDiffLoading(true);
    api
      .getInternalPullRequestCandidateFileDiff(
        candidate.candidate_id,
        selectedFile.path,
      )
      .then((result) => {
        if (!cancelled) setDiff(result.diff);
      })
      .catch((reason) => {
        if (!cancelled) {
          setDiff("");
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      })
      .finally(() => {
        if (!cancelled) setDiffLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeTab, candidate, selectedFile]);

  useEffect(() => {
    setEnd(
      <Button
        ghost
        size="icon"
        type="button"
        onClick={() => setRefreshToken((value) => value + 1)}
        disabled={loading}
        aria-label="Refresh pull request candidates"
      >
        {loading ? <Spinner /> : <RefreshCw />}
      </Button>,
    );
    return () => setEnd(null);
  }, [loading, setEnd]);

  if (!candidateId) {
    return (
      <div className="space-y-4 py-4 text-[#1e293b]">
        {error ? (
          <div role="alert" className="rounded-sm border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            {error}
          </div>
        ) : null}
        <CandidateList
          candidates={candidates}
          loading={loading}
          onSelect={(id) => setSearchParams({ candidate: id })}
        />
      </div>
    );
  }

  if (loading && !candidate) {
    return (
      <div className="flex min-h-72 items-center justify-center">
        <Spinner />
      </div>
    );
  }

  if (!candidate) {
    return (
      <div className="space-y-4 py-4">
        <button
          type="button"
          onClick={() => setSearchParams({})}
          className="inline-flex items-center gap-1 text-xs font-semibold text-[#3b82f6]"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          All candidates
        </button>
        <div role="alert" className="rounded-sm border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error || "The requested candidate was not found."}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4 py-4 text-[#1e293b]">
      <button
        type="button"
        onClick={() => setSearchParams({})}
        className="inline-flex items-center gap-1 text-xs font-semibold text-[#3b82f6]"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        All candidates
      </button>

      {error ? (
        <div role="alert" className="rounded-sm border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      ) : null}

      <section className="rounded-lg border border-[#e2e8f0] bg-white shadow-[0_2px_8px_rgba(0,0,0,0.06)]">
        <header className="px-5 py-4">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-xl font-semibold text-[#1e293b]">{candidate.title}</h2>
            <span
              className={`rounded-full border px-2 py-0.5 text-[11px] font-semibold ${
                candidate.lock_state === "immutable_approved"
                  ? "border-[#16a34a] text-[#16a34a]"
                  : "border-amber-500 text-amber-700"
              }`}
            >
              {lockLabel(candidate)}
            </span>
          </div>
          <p className="mt-2 text-xs text-[#64748b]">
            <span className="font-semibold">{candidate.repository}</span> ·{" "}
            <span className="font-mono">{candidate.head_ref}</span> into{" "}
            <span className="font-mono">{candidate.base_ref}</span>
          </p>
        </header>
        <nav className="flex overflow-x-auto border-t border-[#e2e8f0] px-2" aria-label="Candidate details">
          {TABS.map((tab) => (
            <button
              type="button"
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`whitespace-nowrap border-b-2 px-3 py-3 text-xs font-semibold ${
                activeTab === tab.id
                  ? "border-[#3b82f6] text-[#1e293b]"
                  : "border-transparent text-[#64748b] hover:border-[#e2e8f0]"
              }`}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </section>

      {activeTab === "conversation" ? (
        <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_18rem]">
          <article className="min-h-72 rounded-lg border border-[#e2e8f0] bg-white p-5 shadow-sm">
            <Markdown content={candidate.body} />
          </article>
          <aside className="space-y-3 rounded-lg border border-[#e2e8f0] bg-white p-4 text-xs shadow-sm">
            <p className="font-semibold text-[#1e293b]">Candidate identity</p>
            <dl className="space-y-3 text-[#64748b]">
              <div>
                <dt className="font-semibold">Task</dt>
                <dd className="mt-0.5 font-mono">{candidate.task_id}</dd>
              </div>
              <div>
                <dt className="font-semibold">Base</dt>
                <dd className="mt-0.5 break-all font-mono">{candidate.base_sha}</dd>
              </div>
              <div>
                <dt className="font-semibold">Head</dt>
                <dd className="mt-0.5 break-all font-mono">{candidate.head_sha}</dd>
              </div>
              <div>
                <dt className="font-semibold">Updated</dt>
                <dd className="mt-0.5">{formatDate(candidate.updated_at)}</dd>
              </div>
              {candidate.locked_at ? (
                <div>
                  <dt className="flex items-center gap-1 font-semibold">
                    <LockKeyhole className="h-3.5 w-3.5" />
                    Locked
                  </dt>
                  <dd className="mt-0.5">{formatDate(candidate.locked_at)}</dd>
                </div>
              ) : null}
            </dl>
          </aside>
        </section>
      ) : null}

      {activeTab === "commits" ? (
        <section className="overflow-hidden rounded-lg border border-[#e2e8f0] bg-white shadow-sm">
          <ul>
            {candidate.commits.map((commit) => (
              <li className="flex gap-3 border-b border-[#e2e8f0] px-4 py-4 last:border-b-0" key={commit.sha}>
                <GitCommitHorizontal className="mt-0.5 h-4 w-4 shrink-0 text-[#64748b]" />
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-semibold text-[#1e293b]">{commit.message}</span>
                  <span className="mt-1 block text-xs text-[#64748b]">
                    {commit.author_name} committed {formatDate(commit.authored_at)}
                  </span>
                </span>
                <span className="self-start rounded border border-[#e2e8f0] bg-[#f8fafc] px-2 py-1 font-mono text-[11px]">
                  {commit.sha.slice(0, 7)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {activeTab === "files" ? (
        <section className="grid overflow-hidden rounded-lg border border-[#e2e8f0] bg-white shadow-[0_2px_8px_rgba(0,0,0,0.06)] xl:grid-cols-[20rem_minmax(0,1fr)]">
          <aside className="border-b border-[#e2e8f0] xl:border-b-0 xl:border-r">
            <div className="flex h-12 items-center justify-between border-b border-[#e2e8f0] px-3">
              <div className="flex items-center gap-2 text-xs font-semibold">
                <GitCompare className="h-4 w-4 text-[#3b82f6]" />
                Production files
              </div>
              <span className="rounded-full bg-[#f1f5f9] px-2 py-0.5 text-[11px] text-[#64748b]">
                {candidate.files.length}
              </span>
            </div>
            <FileList
              files={candidate.files}
              selectedPath={selectedPath}
              onSelect={(file) => setSelectedPath(file.path)}
            />
          </aside>
          <div className="min-w-0">
            <div className="flex min-h-12 flex-wrap items-center justify-between gap-2 border-b border-[#e2e8f0] px-3 py-2">
              <div className="min-w-0">
                <p className="truncate font-mono text-xs font-semibold" title={selectedFile?.path}>
                  {selectedFile?.path || "Select a changed file"}
                </p>
                {selectedFile ? (
                  <p className="mt-0.5 text-[10px] text-[#64748b]" title={selectedFile.necessity}>
                    {statusLabel(selectedFile.status)} · +{selectedFile.added} -{selectedFile.removed} · {selectedFile.necessity}
                  </p>
                ) : null}
              </div>
              <div className="flex rounded-md border border-[#e2e8f0] bg-[#f8fafc] p-0.5">
                {(["split", "unified"] as const).map((mode) => (
                  <button
                    type="button"
                    key={mode}
                    onClick={() => setDiffView(mode)}
                    className={`rounded-sm px-3 py-1 text-[11px] font-semibold capitalize ${
                      diffView === mode
                        ? "bg-white text-[#3b82f6] shadow-sm"
                        : "text-[#64748b]"
                    }`}
                  >
                    {mode}
                  </button>
                ))}
              </div>
            </div>
            <div className="max-h-[42rem] min-h-72 overflow-auto bg-white">
              {diffLoading ? (
                <div className="flex min-h-72 items-center justify-center">
                  <Spinner />
                </div>
              ) : diffView === "split" ? (
                <SplitDiff diff={diff} />
              ) : (
                <UnifiedDiff diff={diff} />
              )}
            </div>
          </div>
        </section>
      ) : null}

      {activeTab === "checks" ? (
        <section className="overflow-hidden rounded-lg border border-[#e2e8f0] bg-white shadow-sm">
          {candidate.checks.length ? (
            <ul>
              {candidate.checks.map((check) => (
                <li className="flex gap-3 border-b border-[#e2e8f0] px-4 py-4 last:border-b-0" key={check.name}>
                  <CheckIcon check={check} />
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm font-semibold text-[#1e293b]">{check.name}</span>
                    <span className="mt-1 block text-xs text-[#64748b]">
                      {check.summary || check.conclusion || check.status}
                    </span>
                    {check.validation_overlay_digest ? (
                      <span className="mt-1 block break-all font-mono text-[10px] text-[#94a3b8]">
                        Validation overlay: {check.validation_overlay_digest}
                      </span>
                    ) : null}
                  </span>
                  <span className="self-start text-xs font-semibold capitalize text-[#64748b]">
                    {(check.conclusion || check.status).replaceAll("_", " ")}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <div className="p-6 text-sm text-[#64748b]">No checks have been recorded.</div>
          )}
        </section>
      ) : null}

      {activeTab === "reviews" ? (
        <section className="overflow-hidden rounded-lg border border-[#e2e8f0] bg-white shadow-sm">
          {candidate.reviews.length ? (
            <ul>
              {candidate.reviews.map((review) => (
                <li className="border-b border-[#e2e8f0] px-4 py-4 last:border-b-0" key={review.review_id}>
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="flex items-center gap-2 text-sm font-semibold text-[#1e293b]">
                      <ShieldCheck className="h-4 w-4 text-[#3b82f6]" />
                      {review.role}
                    </span>
                    <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${
                      review.verdict === "APPROVE"
                        ? "bg-emerald-50 text-emerald-700"
                        : "bg-amber-50 text-amber-700"
                    }`}>
                      {review.verdict.replaceAll("_", " ")}
                    </span>
                  </div>
                  <p className="mt-2 text-sm text-[#64748b]">{review.summary}</p>
                  {review.findings.length ? (
                    <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-[#64748b]">
                      {review.findings.map((finding) => <li key={finding}>{finding}</li>)}
                    </ul>
                  ) : null}
                  <p className="mt-2 text-[11px] text-[#94a3b8]">
                    {review.reviewer} · {formatDate(review.submitted_at)}
                  </p>
                </li>
              ))}
            </ul>
          ) : (
            <div className="p-6 text-sm text-[#64748b]">No reviews have been recorded.</div>
          )}
        </section>
      ) : null}

      {activeTab === "comparison" ? (
        candidate.lock_state !== "immutable_approved" ? (
          <section className="rounded-lg border border-[#e2e8f0] bg-white p-8 text-center text-sm text-[#64748b] shadow-sm">
            reference PR withheld
          </section>
        ) : candidate.post_lock_comparison ? (
          <section className="space-y-4 rounded-lg border border-[#e2e8f0] bg-white p-5 shadow-sm">
            <p className="text-sm text-[#1e293b]">{candidate.post_lock_comparison.summary}</p>
            {([
              ["Defects", candidate.post_lock_comparison.defects],
              ["Coverage", candidate.post_lock_comparison.coverage],
              ["Minimality", candidate.post_lock_comparison.minimality],
            ] as const).map(([label, items]) => (
              <div key={label}>
                <h3 className="text-xs font-semibold uppercase tracking-[0.1em] text-[#64748b]">{label}</h3>
                {items.length ? (
                  <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-[#64748b]">
                    {items.map((item) => <li key={item}>{item}</li>)}
                  </ul>
                ) : (
                  <p className="mt-2 text-sm text-[#94a3b8]">No findings.</p>
                )}
              </div>
            ))}
          </section>
        ) : (
          <section className="rounded-md border border-[#e2e8f0] bg-white p-6 text-sm text-[#64748b] shadow-sm">
            No post-lock comparison has been recorded.
          </section>
        )
      ) : null}

      {activeTab === "accounting" ? (
        <div className="space-y-4">
          <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Metric
              label="Wall clock"
              value={formatDuration(candidate.accounting.wall_clock_duration_ms)}
              detail="Elapsed candidate time"
            />
            <Metric
              label="Active work"
              value={formatDuration(candidate.accounting.active_duration_ms)}
              detail="Summed execution and model work"
            />
            <Metric
              label="Estimated cost"
              value={`$${candidate.accounting.estimated_llm_cost_usd.toFixed(4)}`}
              detail={candidate.accounting.cost_complete ? "Cost complete" : "Estimate incomplete"}
            />
            <Metric
              label="Rounds"
              value={candidate.accounting.rounds.length}
              detail="Persisted numeric work records"
            />
          </section>
          <section className="overflow-hidden rounded-lg border border-[#e2e8f0] bg-white shadow-sm">
            {candidate.accounting.rounds.length ? (
              <ul>
                {candidate.accounting.rounds.map((round) => (
                  <li className="grid gap-2 border-b border-[#e2e8f0] px-4 py-3 text-xs last:border-b-0 md:grid-cols-[minmax(0,1fr)_8rem_8rem_8rem]" key={round.round_id}>
                    <span>
                      <span className="block font-semibold text-[#1e293b]">{round.kind}</span>
                      <span className="mt-0.5 block text-[#64748b]">{round.role} · {round.outcome}</span>
                    </span>
                    <span>{formatDuration(round.duration_ms)}</span>
                    <span>{round.input_tokens + round.output_tokens} tokens</span>
                    <span>{round.estimated_cost_usd === null ? "Unknown" : `$${round.estimated_cost_usd.toFixed(4)}`}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <div className="p-6 text-sm text-[#64748b]">No accounting rounds have been recorded.</div>
            )}
          </section>
        </div>
      ) : null}
    </div>
  );
}
