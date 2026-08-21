// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CompletedSolutionsCards, CompletedSolutionsTable } from "./CompletedSolutions";
import { COMPLETED_SOLUTIONS } from "@/lib/completed-solutions";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      return new Response(
        JSON.stringify({
          statuses: COMPLETED_SOLUTIONS.map((solution) => ({
            key: solution.id,
            state:
              solution.pullRequestNumber === 568 ||
              solution.pullRequestNumber === 569
                ? "merged"
                : "open",
            number: solution.pullRequestNumber,
            url: solution.pullRequestUrl,
            checked_at: "2026-08-21T00:00:00Z",
          })),
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }),
  );
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

describe("CompletedSolutions", () => {
  it.each([
    ["table", <CompletedSolutionsTable />],
    ["cards", <CompletedSolutionsCards />],
  ])("renders every completed solution in the %s view", async (_name, view) => {
    await act(async () => root.render(view));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.querySelectorAll("[data-completed-solution]")).toHaveLength(
      COMPLETED_SOLUTIONS.length,
    );
    expect(container.querySelectorAll('[data-solution-status="done"]')).toHaveLength(
      COMPLETED_SOLUTIONS.length,
    );
    for (const solution of COMPLETED_SOLUTIONS) {
      const link = container.querySelector<HTMLAnchorElement>(
        `a[href="${solution.pullRequestUrl}"]`,
      );
      expect(link?.textContent).toContain(`#${solution.pullRequestNumber}`);
    }
    expect(container.querySelectorAll('[data-upstream-state="merged"]')).toHaveLength(2);
    expect(container.querySelectorAll('[aria-label="Live GitHub status"]')).toHaveLength(
      COMPLETED_SOLUTIONS.length,
    );
    expect(fetch).toHaveBeenCalledWith(
      "/api/v2/project-hermes/github/pull-request-statuses",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("keeps the last known state when one backend lookup is unknown", async () => {
    let request = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        request += 1;
        return new Response(
          JSON.stringify({
            statuses: COMPLETED_SOLUTIONS.map((solution) => ({
              key: solution.id,
              state:
                request > 1 && solution.id === "spur-568" ? "unknown" : "open",
              number: solution.pullRequestNumber,
              url: solution.pullRequestUrl,
              checked_at: "2026-08-21T00:00:00Z",
            })),
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }),
    );

    await act(async () => root.render(<CompletedSolutionsCards />));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const solution = container.querySelector(
      '[data-completed-solution="spur-568"]',
    );
    expect(solution?.querySelector('[data-upstream-state="open"]')).not.toBeNull();

    const refresh = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Refresh GitHub pull request states"]',
    );
    await act(async () => {
      refresh?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(solution?.querySelector('[data-upstream-state="open"]')).not.toBeNull();
    expect(
      solution?.querySelector('[aria-label="Status not yet refreshed"]'),
    ).not.toBeNull();
  });
});
