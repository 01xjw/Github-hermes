export type DiffCellKind = "context" | "addition" | "deletion";

export interface DiffCell {
  kind: DiffCellKind;
  lineNumber: number;
  text: string;
}

export interface SplitDiffRow {
  kind: "content" | "meta";
  left: DiffCell | null;
  right: DiffCell | null;
  text?: string;
}

export interface SplitDiffHunk {
  header: string;
  oldStart: number;
  newStart: number;
  rows: SplitDiffRow[];
}

export interface UnifiedDiffDocument {
  headers: string[];
  hunks: SplitDiffHunk[];
}

const HUNK_HEADER =
  /^@@ -(?<oldStart>\d+)(?:,\d+)? \+(?<newStart>\d+)(?:,\d+)? @@/;

function contentCell(
  kind: DiffCellKind,
  lineNumber: number,
  line: string,
): DiffCell {
  return {
    kind,
    lineNumber,
    text: line.slice(1),
  };
}

function buildRows(
  lines: string[],
  oldStart: number,
  newStart: number,
): SplitDiffRow[] {
  const rows: SplitDiffRow[] = [];
  let oldLine = oldStart;
  let newLine = newStart;
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    if (line.startsWith(" ")) {
      rows.push({
        kind: "content",
        left: contentCell("context", oldLine, line),
        right: contentCell("context", newLine, line),
      });
      oldLine += 1;
      newLine += 1;
      index += 1;
      continue;
    }

    if (line.startsWith("-") || line.startsWith("+")) {
      const deletions: DiffCell[] = [];
      const additions: DiffCell[] = [];

      while (
        index < lines.length &&
        (lines[index].startsWith("-") || lines[index].startsWith("+"))
      ) {
        const changedLine = lines[index];
        if (changedLine.startsWith("-")) {
          deletions.push(contentCell("deletion", oldLine, changedLine));
          oldLine += 1;
        } else {
          additions.push(contentCell("addition", newLine, changedLine));
          newLine += 1;
        }
        index += 1;
      }

      const rowCount = Math.max(deletions.length, additions.length);
      for (let rowIndex = 0; rowIndex < rowCount; rowIndex += 1) {
        rows.push({
          kind: "content",
          left: deletions[rowIndex] ?? null,
          right: additions[rowIndex] ?? null,
        });
      }
      continue;
    }

    rows.push({
      kind: "meta",
      left: null,
      right: null,
      text: line,
    });
    index += 1;
  }

  return rows;
}

export function parseUnifiedDiff(diff: string): UnifiedDiffDocument {
  const headers: string[] = [];
  const hunks: SplitDiffHunk[] = [];
  const lines = diff.replaceAll("\r\n", "\n").split("\n");
  let current:
    | {
        header: string;
        oldStart: number;
        newStart: number;
        lines: string[];
      }
    | undefined;

  const flush = () => {
    if (!current) return;
    hunks.push({
      header: current.header,
      oldStart: current.oldStart,
      newStart: current.newStart,
      rows: buildRows(current.lines, current.oldStart, current.newStart),
    });
    current = undefined;
  };

  for (const line of lines) {
    const match = line.match(HUNK_HEADER);
    if (match?.groups) {
      flush();
      current = {
        header: line,
        oldStart: Number(match.groups.oldStart),
        newStart: Number(match.groups.newStart),
        lines: [],
      };
      continue;
    }

    if (current) {
      current.lines.push(line);
    } else if (line) {
      headers.push(line);
    }
  }

  flush();
  return { headers, hunks };
}

export function unifiedLineKind(
  line: string,
): "header" | "hunk" | DiffCellKind | "meta" {
  if (line.startsWith("@@")) return "hunk";
  if (
    line.startsWith("diff ") ||
    line.startsWith("index ") ||
    line.startsWith("---") ||
    line.startsWith("+++")
  ) {
    return "header";
  }
  if (line.startsWith("+")) return "addition";
  if (line.startsWith("-")) return "deletion";
  if (line.startsWith(" ")) return "context";
  return "meta";
}
