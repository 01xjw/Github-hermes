"""Deterministic AMD-or-portable Issue relevance checks."""

from __future__ import annotations

import re
from collections.abc import Iterable

from project_hermes.config import IssueRelevancePolicy


_AMD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("amd", re.compile(r"\bamd\b", re.IGNORECASE)),
    ("rocm", re.compile(r"\brocm\b", re.IGNORECASE)),
    ("radeon", re.compile(r"\bradeon\b", re.IGNORECASE)),
    ("amdgpu", re.compile(r"\bamdgpu\b", re.IGNORECASE)),
    (
        "hip",
        re.compile(
            r"\bhip(?:ify|blas|fft|rand|solver|sparse)?\b",
            re.IGNORECASE,
        ),
    ),
    ("gfx", re.compile(r"\bgfx[0-9a-z]+\b", re.IGNORECASE)),
    (
        "instinct",
        re.compile(
            r"\b(?:instinct|mi(?:100|200|210|250|300|308|325|350))\b",
            re.IGNORECASE,
        ),
    ),
    ("rdna", re.compile(r"\b(?:rdna|cdna)[0-9]*\b", re.IGNORECASE)),
    (
        "roc library",
        re.compile(
            r"\b(?:rocblas|rocfft|rocrand|rocsolver|rocsparse|miopen|rccl)\b",
            re.IGNORECASE,
        ),
    ),
    ("aiter", re.compile(r"\baiter\b", re.IGNORECASE)),
    ("composable kernel", re.compile(r"\bcomposable[ _-]kernel\b", re.IGNORECASE)),
)

_NON_AMD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("nvidia", re.compile(r"\bnvidia\b", re.IGNORECASE)),
    (
        "cuda",
        re.compile(
            r"\bcuda(?:[ _-]?[0-9]+(?:\.[0-9]+)?)?\b",
            re.IGNORECASE,
        ),
    ),
    ("cudnn", re.compile(r"\bcudnn\b", re.IGNORECASE)),
    ("cublas", re.compile(r"\bcublas(?:lt)?\b", re.IGNORECASE)),
    ("cutlass", re.compile(r"\bcutlass\b", re.IGNORECASE)),
    ("tensorrt", re.compile(r"\btensorrt(?:[ _-]llm)?\b", re.IGNORECASE)),
    ("nccl", re.compile(r"\bnccl\b", re.IGNORECASE)),
    ("h20", re.compile(r"\bh20\b", re.IGNORECASE)),
    ("h100", re.compile(r"\bh100\b", re.IGNORECASE)),
    ("h200", re.compile(r"\bh200\b", re.IGNORECASE)),
    ("a100", re.compile(r"\ba100\b", re.IGNORECASE)),
    ("a800", re.compile(r"\ba800\b", re.IGNORECASE)),
    ("l40", re.compile(r"\bl40s?\b", re.IGNORECASE)),
    ("b100", re.compile(r"\bb100\b", re.IGNORECASE)),
    ("b200", re.compile(r"\bb200\b", re.IGNORECASE)),
    ("gb200", re.compile(r"\bgb200\b", re.IGNORECASE)),
    (
        "compute capability",
        re.compile(
            r"\bsm(?:7[05]|8[069]|89|9[0a]|100|120)\b",
            re.IGNORECASE,
        ),
    ),
    ("blackwell", re.compile(r"\bblackwell\b", re.IGNORECASE)),
    ("hopper", re.compile(r"\bhopper\b", re.IGNORECASE)),
    ("ampere", re.compile(r"\bampere\b", re.IGNORECASE)),
    ("intel", re.compile(r"\bintel\b", re.IGNORECASE)),
    ("xpu", re.compile(r"\bxpu\b", re.IGNORECASE)),
    ("oneapi", re.compile(r"\boneapi\b", re.IGNORECASE)),
    ("gaudi", re.compile(r"\bgaudi[0-9]*\b", re.IGNORECASE)),
    ("hpu", re.compile(r"\bhpu\b", re.IGNORECASE)),
    ("ascend", re.compile(r"\bascend\b", re.IGNORECASE)),
    (
        "apple mps",
        re.compile(
            r"\b(?:apple[ _-]mps|mps backend)\b",
            re.IGNORECASE,
        ),
    ),
    ("tpu", re.compile(r"\btpu\b", re.IGNORECASE)),
)

_PORTABLE_EXECUTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:\[cpu\]|\b(?:cpu[- ]?only|cpu[- ]?specific|"
        r"cpu[- ]?(?:repro|reproducer)|"
        r"reproduc(?:e[sd]?|ible)\s+(?:on|with)\s+(?:the\s+)?cpu|"
        r"without\s+(?:an?\s+)?(?:gpu|accelerator)|"
        r"no\s+(?:gpu|accelerator)\s+is\s+required)\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:backend|hardware|accelerator)[ _-]?(?:agnostic|independent)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:fails?|failed|broken|crash(?:es|ed)?|hangs?|incorrect|"
        r"regression|wrong\s+(?:result|gradient)s?)\b.{0,64}"
        r"\b(?:on|with|using|for)\b.{0,24}\bcpu\b|"
        r"\bcpu\b.{0,64}\b(?:fails?|failed|broken|crash(?:es|ed)?|hangs?|"
        r"incorrect|regression|wrong\s+(?:result|gradient)s?)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:both|either)\s+(?:the\s+)?cpu\s+(?:and|or)\s+"
        r"(?:cuda|gpu|xpu|accelerator)\b|"
        r"\bcpu\s+(?:and|or)\s+(?:cuda|gpu|xpu|accelerator)\b|"
        r"\b(?:cuda|gpu|xpu|accelerator)\s+(?:and|or)\s+cpu\b",
        re.IGNORECASE,
    ),
)


def issue_relevance_reasons(
    *,
    policy: IssueRelevancePolicy,
    title: str,
    body: str,
    labels: Iterable[str],
) -> list[str]:
    """Return mechanical exclusion reasons for non-AMD-specific Issues.

    AMD-owned repositories opt into ``amd_native``. Other repositories accept
    explicit AMD/ROCm work and hardware-neutral work. They reject Issues whose
    titles that name another accelerator and explicit non-AMD requirements
    found in the body are excluded. Labels are ownership/routing metadata and
    never prove an execution requirement by themselves. An executable CPU or
    hardware-neutral path wins over vendor words anywhere in the report.
    Generic environment/model metadata does not by itself exclude an
    otherwise portable Issue.
    """

    if policy in {
        IssueRelevancePolicy.DISABLED,
        IssueRelevancePolicy.AMD_NATIVE,
    }:
        return []

    normalized_labels = [
        str(label).strip() for label in labels if str(label).strip()
    ]
    title_and_labels = "\n".join([title, *normalized_labels])
    all_text = f"{title_and_labels}\n{body}"
    if _signals(all_text, _AMD_PATTERNS):
        return []
    if _has_portable_execution_path(all_text):
        return []

    specific = set(_signals(title, _NON_AMD_PATTERNS))
    body_signals = set(_signals(body, _NON_AMD_PATTERNS))
    specific.update(_exclusive_body_signals(body, body_signals))
    if not specific:
        return []
    return [
        "non-AMD hardware-specific Issue without an AMD/ROCm signal: "
        + ", ".join(sorted(specific))
    ]


def _signals(
    text: str,
    patterns: tuple[tuple[str, re.Pattern[str]], ...],
) -> list[str]:
    return [name for name, pattern in patterns if pattern.search(text)]


def _has_portable_execution_path(text: str) -> bool:
    """Recognize explicit CPU/general paths before vendor exclusions.

    The patterns intentionally require a runnable/failing CPU path or an
    explicit hardware-neutral statement. A sentence such as "CPU works but
    CUDA fails" is not enough to override a CUDA-only Issue.
    """

    return any(pattern.search(text) for pattern in _PORTABLE_EXECUTION_PATTERNS)


def _exclusive_body_signals(body: str, signals: set[str]) -> set[str]:
    if not signals:
        return set()
    exclusive: set[str] = set()
    for name, pattern in _NON_AMD_PATTERNS:
        if name not in signals:
            continue
        for match in pattern.finditer(body):
            window = _clause_containing(body, match.start(), match.end())
            signal = re.escape(match.group(0).casefold())
            explicit_constraint = re.search(
                rf"(?:\b(?:only|specific(?:ally)?|requires?|required|"
                rf"unsupported)\b.{{0,48}}{signal}|{signal}.{{0,48}}"
                rf"\b(?:only|specific|exclusive|required|unsupported)\b)",
                window,
            )
            failure_on_hardware = re.search(
                rf"(?:\b(?:fails?|broken|crash(?:es|ed)?|hangs?|"
                rf"incorrect|regression|does\s+not\s+work)\b.{{0,40}}"
                rf"\b(?:on|with|using|for)\b.{{0,24}}{signal}|"
                rf"{signal}.{{0,48}}\b(?:fails?|broken|crash(?:es|ed)?|"
                rf"hangs?|incorrect|regression|does\s+not\s+work)\b)",
                window,
            )
            if explicit_constraint or failure_on_hardware:
                exclusive.add(name)
                break
    return exclusive


def _clause_containing(body: str, start: int, end: int) -> str:
    """Keep hardware constraints from leaking across report sections."""

    delimiters = "\n.;!?"
    left = max(body.rfind(delimiter, 0, start) for delimiter in delimiters)
    right_candidates = [
        index
        for delimiter in delimiters
        if (index := body.find(delimiter, end)) >= 0
    ]
    right = min(right_candidates, default=len(body))
    return " ".join(body[left + 1 : right].casefold().split())


__all__ = ["issue_relevance_reasons"]
