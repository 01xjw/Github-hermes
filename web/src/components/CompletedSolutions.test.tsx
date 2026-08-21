// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { CompletedSolutionsCards, CompletedSolutionsTable } from "./CompletedSolutions";
import { COMPLETED_SOLUTIONS } from "@/lib/completed-solutions";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe("CompletedSolutions", () => {
  it.each([
    ["table", <CompletedSolutionsTable />],
    ["cards", <CompletedSolutionsCards />],
  ])("renders every completed solution in the %s view", async (_name, view) => {
    await act(async () => root.render(view));

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
  });
});
