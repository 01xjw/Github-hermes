import type { ProjectPullRequestStatus } from "@/lib/api";

export type UpstreamPullRequestState = Exclude<
  ProjectPullRequestStatus["state"],
  "unknown"
>;

export interface CompletedSolution {
  id: string;
  repository: string;
  pullRequestNumber: number;
  pullRequestUrl: string;
  title: string;
  summary: string;
  upstreamState: UpstreamPullRequestState;
}

export const COMPLETED_SOLUTIONS = [
  {
    id: "spur-568",
    repository: "ROCm/spur",
    pullRequestNumber: 568,
    pullRequestUrl: "https://github.com/ROCm/spur/pull/568",
    title: "Node commands support ALL",
    summary:
      "Added case-insensitive ALL support to node label, drain, and remove, with batch-operation test coverage.",
    upstreamState: "merged",
  },
  {
    id: "spur-569",
    repository: "ROCm/spur",
    pullRequestNumber: 569,
    pullRequestUrl: "https://github.com/ROCm/spur/pull/569",
    title: "Fail-closed username validation",
    summary:
      "Hardened username parsing so unresolved users cannot submit or operate jobs; the change is merged upstream.",
    upstreamState: "merged",
  },
  {
    id: "aiter-4648",
    repository: "ROCm/AITER",
    pullRequestNumber: 4648,
    pullRequestUrl: "https://github.com/ROCm/aiter/pull/4648",
    title: "gfx1100 A8W8 GEMM tuning",
    summary:
      "Optimized RDNA3 gfx1100 A8W8 GEMM configurations for AITER operator scheduling and acceleration.",
    upstreamState: "open",
  },
  {
    id: "vllm-51598",
    repository: "vllm-project/vllm",
    pullRequestNumber: 51598,
    pullRequestUrl: "https://github.com/vllm-project/vllm/pull/51598",
    title: "RDNA3 AITER inference paths",
    summary:
      "Adapted AITER W8A8, GDN decoding, and sampling paths on RDNA3 to expand vLLM model inference support.",
    upstreamState: "open",
  },
] satisfies readonly CompletedSolution[];
