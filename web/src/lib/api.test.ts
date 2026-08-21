// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, fetchJSON } from "./api";

const reloadMocks = vi.hoisted(() => ({
  attemptDashboardTokenReloadOnce: vi.fn(() => false),
  clearDashboardTokenReloadAttempt: vi.fn(),
}));

vi.mock("./dashboard-auth-reload", () => ({
  attemptDashboardTokenReloadOnce: reloadMocks.attemptDashboardTokenReloadOnce,
  clearDashboardTokenReloadAttempt: reloadMocks.clearDashboardTokenReloadAttempt,
}));

const SESSION_HEADER = "X-Hermes-Session-Token";

beforeEach(() => {
  reloadMocks.attemptDashboardTokenReloadOnce.mockReset();
  reloadMocks.attemptDashboardTokenReloadOnce.mockReturnValue(false);
  reloadMocks.clearDashboardTokenReloadAttempt.mockReset();

  Object.defineProperty(window, "__HERMES_SESSION_TOKEN__", {
    configurable: true,
    value: "stale-token",
    writable: true,
  });
  Object.defineProperty(window, "__HERMES_AUTH_REQUIRED__", {
    configurable: true,
    value: false,
    writable: true,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function jsonFetchMock(body: unknown = { ok: true }) {
  return vi.fn<typeof fetch>(
    async () =>
      new Response(JSON.stringify(body), {
        headers: { "Content-Type": "application/json" },
        status: 200,
      }),
  );
}

describe("fetchJSON", () => {
  it("tries the one-shot reload path for loopback 401s", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        clone: () => ({
          json: async () => ({}),
        }),
        ok: false,
        status: 401,
        statusText: "Unauthorized",
        text: async () => "Unauthorized",
      })),
    );
    reloadMocks.attemptDashboardTokenReloadOnce.mockReturnValue(true);

    const pending = fetchJSON("/api/status");
    await expect(Promise.race([pending, Promise.resolve("pending")])).resolves.toBe(
      "pending",
    );

    expect(reloadMocks.attemptDashboardTokenReloadOnce).toHaveBeenCalledTimes(1);
    expect(reloadMocks.clearDashboardTokenReloadAttempt).not.toHaveBeenCalled();
  });

  it("clears the reload latch after a successful response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        json: async () => ({ ok: true }),
        ok: true,
        status: 200,
      })),
    );

    await expect(fetchJSON("/api/status")).resolves.toEqual({ ok: true });

    expect(reloadMocks.clearDashboardTokenReloadAttempt).toHaveBeenCalledTimes(1);
  });
});

describe("api.getModelOptions", () => {
  it("requests a live model refresh when asked", async () => {
    vi.stubGlobal("window", {});

    const fetchMock = jsonFetchMock({ providers: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.getModelOptions({ refresh: true });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/model/options?refresh=1&include_unconfigured=1",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("keeps explicit profile scoping when refreshing", async () => {
    vi.stubGlobal("window", {});

    const fetchMock = jsonFetchMock({ providers: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.getModelOptions({ profile: "default", refresh: true });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/model/options?profile=default&refresh=1&include_unconfigured=1",
      expect.objectContaining({ credentials: "include" }),
    );
  });
});

describe("internal pull request candidate API", () => {
  it("uses the authenticated ProjectHermes list, detail, and diff routes", async () => {
    vi.stubGlobal("window", {});
    const fetchMock = jsonFetchMock({});
    vi.stubGlobal("fetch", fetchMock);

    await api.listInternalPullRequestCandidates(25, 5);
    await api.getInternalPullRequestCandidate("candidate/17");
    await api.getInternalPullRequestCandidateFileDiff(
      "candidate/17",
      "src/kernel file.py",
    );

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/v2/project-hermes/pull-request-candidates?limit=25&offset=5",
      "/api/v2/project-hermes/pull-request-candidates/candidate%2F17",
      "/api/v2/project-hermes/pull-request-candidates/candidate%2F17/files/diff?path=src%2Fkernel%20file.py",
    ]);
    for (const call of fetchMock.mock.calls) {
      expect(call[1]).toEqual(
        expect.objectContaining({ credentials: "include" }),
      );
    }
  });
});

describe("ProjectHermes polling API", () => {
  it("uses repository, Issue, Work, and manual polling routes", async () => {
    vi.stubGlobal("window", {});
    const fetchMock = jsonFetchMock({});
    vi.stubGlobal("fetch", fetchMock);

    await api.getPollingOverview(123);
    await api.listProjectIssues({
      repositoryId: 123,
      eligible: true,
      search: "kernel 17",
      status: ["queued", "running"],
      limit: 25,
      offset: 50,
    });
    await api.getProjectIssue("candidate/17");
    await api.rescreenProjectIssue("candidate/17", {
      reason: "The bounded production-worker probe completed.",
      probe_evidence: ["The CPU reproducer passed through the HIP runtime."],
    });
    await api.selectProjectIssue("candidate/17");
    await api.listProjectWorkItems({
      repositoryId: 123,
      status: ["review"],
      limit: 10,
      offset: 20,
    });
    await api.getProjectWorkItem("work/17");
    await api.runPollingNow();

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/v2/project-hermes/polling?repository_id=123",
      "/api/v2/project-hermes/issues?repository_id=123&eligible=true&search=kernel+17&status=queued&status=running&limit=25&offset=50",
      "/api/v2/project-hermes/issues/candidate%2F17",
      "/api/v2/project-hermes/issues/candidate%2F17/rescreen",
      "/api/v2/project-hermes/issues/candidate%2F17/select",
      "/api/v2/project-hermes/work-items?repository_id=123&status=review&limit=10&offset=20",
      "/api/v2/project-hermes/work-items/work%2F17",
      "/api/v2/project-hermes/polling/run",
    ]);
    expect(fetchMock.mock.calls[3]?.[1]).toEqual(
      expect.objectContaining({
        body: JSON.stringify({
          schema_version: "rescreen-issue-request.v1",
          reason: "The bounded production-worker probe completed.",
          probe_evidence: ["The CPU reproducer passed through the HIP runtime."],
        }),
        credentials: "include",
        method: "POST",
      }),
    );
    expect(fetchMock.mock.calls[4]?.[1]).toEqual(
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock.mock.calls.at(-1)?.[1]).toEqual(
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
  });
});

describe("api OAuth helpers", () => {
  it("starts OAuth login in gated mode without requiring an injected session token", async () => {
    vi.stubGlobal("window", { __HERMES_AUTH_REQUIRED__: true });
    const fetchMock = jsonFetchMock({
      flow: "device_code",
      session_id: "oauth-session",
    });
    vi.stubGlobal("fetch", fetchMock);

    await api.startOAuthLogin("openai-codex");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/providers/oauth/openai-codex/start",
      expect.objectContaining({
        body: "{}",
        credentials: "include",
        method: "POST",
      }),
    );
    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.has(SESSION_HEADER)).toBe(false);
  });

  it("still sends the injected session token for OAuth login in loopback mode", async () => {
    vi.stubGlobal("window", { __HERMES_SESSION_TOKEN__: "loopback-token" });
    const fetchMock = jsonFetchMock({
      flow: "device_code",
      session_id: "oauth-session",
    });
    vi.stubGlobal("fetch", fetchMock);

    await api.startOAuthLogin("openai-codex");

    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get(SESSION_HEADER)).toBe("loopback-token");
  });

  it("runs provider auth mutations in gated mode via cookie auth", async () => {
    vi.stubGlobal("window", { __HERMES_AUTH_REQUIRED__: true });
    const fetchMock = jsonFetchMock({ ok: true });
    vi.stubGlobal("fetch", fetchMock);

    await api.disconnectOAuthProvider("anthropic");
    await api.submitOAuthCode("anthropic", "oauth-session", "code-123");
    await api.cancelOAuthSession("oauth-session");
    await api.revealEnvVar("OPENAI_API_KEY");

    for (const call of fetchMock.mock.calls) {
      const init = call[1] as RequestInit;
      expect(init.credentials).toBe("include");
      expect((init.headers as Headers).has(SESSION_HEADER)).toBe(false);
    }
  });
});
