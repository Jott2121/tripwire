"""Helpers for harness/runner tests: scripted agents for real tasks. No network, no clock."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from tripwire import synthdata
from tripwire import tasks as tk
from tripwire.faults import FaultKind
from tripwire.store import Store
from tripwire.xai import (
    ChatResponse,
    ExecPath,
    ModelConfig,
    Pricing,
    ScriptedClient,
    ToolCallRequest,
)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
CLOCK = lambda: NOW  # noqa: E731
MODEL = ModelConfig("test-model", "test-model-id", "test-effort", "flagship-high")
PRICE = Pricing(50.0, 0.0, "test fixture, not an xAI price")  # 20k input tokens = 1.0 USD
TASKS = tk.default_tasks()
BY_ID = {t.task_id: t for t in TASKS}


def first_task(kind: tk.TaskKind, fault: FaultKind | None = None) -> tk.Task:
    return next(t for t in TASKS if t.kind is kind and (fault is None or fault in t.faults))


def plan(task: tk.Task, fault: FaultKind | None, seed: int = 7) -> tk.PlannedRun:
    return tk.PlannedRun(f"{task.task_id}-r01", task.task_id, fault, seed)


def read_calls(task: tk.Task) -> tuple[ToolCallRequest, ...]:
    """The task's calls that need no earlier result."""
    return tuple(
        ToolCallRequest(f"c{i}", c.tool, c.args)
        for i, c in enumerate(task.calls)
        if tk.WO_ID_ARG not in c.args.values()
    )


def truth_json(task: tk.Task) -> str:
    truth = tk.ground_truth(task, tk.default_store())
    if task.kind is tk.TaskKind.WORK_ORDER:  # the id is issued at write time in a fresh store
        store = Store.from_dataset(synthdata.generate(tk.TASK_DATA_SEED))
        truth["work_order_id"] = store.create_work_order(
            task.key[0], task.action, tk.CREATED_BY, NOW
        ).work_order_id
    return json.dumps(truth)


def refusal_json(*flags: str) -> str:
    return json.dumps(
        {
            "status": "refused",
            "value": None,
            "unit": None,
            "work_order_id": None,
            "flags": list(flags),
        }
    )


def resp(content: str = "", calls=(), remote=(), tokens=(10, 5), model="served-id"):
    return ChatResponse(model, content, tokens[0], tokens[1], tuple(calls), tuple(remote))


def agent_script(task: tk.Task, path: ExecPath, final: str, tokens=(10, 5)) -> list[ChatResponse]:
    """Read what the task needs, then answer ``final``."""
    calls = read_calls(task)
    if path is ExecPath.SERVER_SIDE:
        return [resp(final, remote=calls, tokens=tokens)]
    return [resp(calls=calls, tokens=tokens), resp(final, tokens=tokens)]


def client_for(*scripts: list[ChatResponse]) -> ScriptedClient:
    return ScriptedClient([step for script in scripts for step in script])
