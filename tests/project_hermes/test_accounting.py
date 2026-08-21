from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from project_hermes.accounting import (
    AccountingCostStatus,
    SqliteAccountingStore,
    build_execution_accounting_record,
    build_runtime_accounting_record,
    normalize_runtime_usage,
    summarize_accounting,
)
from project_hermes.config import ModelReasoningMode
from project_hermes.models import ProjectRole
from project_hermes.runtime.base import (
    RuntimeModelRoute,
    RuntimeResult,
    RuntimeStatus,
)


_START = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)


def _route(model: str = "kimi-k3") -> RuntimeModelRoute:
    return RuntimeModelRoute(
        profile="codex-primary",
        model=model,
        model_provider="digitalocean",
        provider_endpoint="https://inference.do-ai.run/v1",
        provider_api_key_env="MODEL_ACCESS_KEY",
        provider_wire_api="chat",
        reasoning_mode=ModelReasoningMode.PROVIDER_DEFAULT,
    )


def _result(model: str = "kimi-k3") -> RuntimeResult:
    return RuntimeResult(
        session_id="codex-session-1",
        status=RuntimeStatus.COMPLETED,
        native_turn_id="turn-1",
        model_route=_route(model),
        output={
            "usage": {
                "prompt_tokens": 1_000,
                "completion_tokens": 100,
                "completion_tokens_details": {
                    "reasoning_tokens": 50,
                },
            }
        },
        started_at=_START,
        completed_at=_START + timedelta(seconds=10),
        duration_ms=10_000,
    )


def test_runtime_usage_prices_digitalocean_route() -> None:
    record = build_runtime_accounting_record(
        task_id="task-1",
        run_id="run-1",
        node_id="implementation",
        role=ProjectRole.CODEX,
        review_cycle=1,
        runtime_name="codex-task",
        result=_result(),
    )

    assert record.input_tokens == 1_000
    assert record.output_tokens == 100
    assert record.reasoning_tokens == 50
    assert record.api_calls == 1
    assert record.cost_status is AccountingCostStatus.ESTIMATED
    assert record.estimated_cost_usd == pytest.approx(0.004275)


def test_unknown_pricing_is_explicit() -> None:
    record = build_runtime_accounting_record(
        task_id="task-1",
        run_id="run-1",
        node_id="implementation",
        role=ProjectRole.CODEX,
        review_cycle=1,
        runtime_name="codex-task",
        result=_result("future-unpriced-model"),
    )

    assert record.cost_status is AccountingCostStatus.UNKNOWN
    assert record.estimated_cost_usd is None


def test_chat_usage_splits_cache_and_reasoning_token_buckets() -> None:
    usage = normalize_runtime_usage(
        {
            "usage": {
                "prompt_tokens": 1_000,
                "prompt_tokens_details": {"cached_tokens": 400},
                "completion_tokens": 100,
                "completion_tokens_details": {"reasoning_tokens": 50},
                "request_count": 2,
            }
        },
        provider="digitalocean",
        wire_api="chat",
    )

    assert usage.input_tokens == 600
    assert usage.cache_read_tokens == 400
    assert usage.output_tokens == 100
    assert usage.reasoning_tokens == 50
    assert usage.request_count == 2


def test_summary_separates_wall_clock_from_active_time() -> None:
    model_record = build_runtime_accounting_record(
        task_id="task-1",
        run_id="run-1",
        node_id="implementation",
        role=ProjectRole.CODEX,
        review_cycle=1,
        runtime_name="codex-task",
        result=_result(),
    )
    execution_record = build_execution_accounting_record(
        task_id="task-1",
        run_id="run-1",
        node_id="validation",
        execution_id="execution-1",
        outcome="SUCCEEDED",
        started_at=_START + timedelta(seconds=5),
        completed_at=_START + timedelta(seconds=15),
    )

    summary = summarize_accounting(
        task_id="task-1",
        run_id="run-1",
        task_started_at=_START,
        task_completed_at=_START + timedelta(seconds=15),
        records=[model_record, execution_record],
    )

    assert summary.wall_clock_duration_ms == 15_000
    assert summary.active_duration_ms == 20_000
    assert summary.estimated_llm_cost_usd == pytest.approx(0.004275)
    assert summary.cost_complete


def test_accounting_store_is_append_only_and_idempotent(
    tmp_path: Path,
) -> None:
    store = SqliteAccountingStore(tmp_path / "accounting.db")
    record = build_runtime_accounting_record(
        task_id="task-1",
        run_id="run-1",
        node_id="implementation",
        role=ProjectRole.CODEX,
        review_cycle=1,
        runtime_name="codex-task",
        result=_result(),
    )

    store.append(record)
    reconstructed = build_runtime_accounting_record(
        task_id="task-1",
        run_id="run-1",
        node_id="implementation",
        role=ProjectRole.CODEX,
        review_cycle=1,
        runtime_name="codex-task",
        result=_result(),
    )
    store.append(reconstructed)

    assert store.list_records("task-1", run_id="run-1") == [record]
