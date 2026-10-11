"""Agent-facing prompt text (M4): the task plus how to report, in the adjudicator's vocabulary.

The baseline wording is deliberately neutral. It names the words a flag can use
(``adjudicator.FLAG_VOCABULARY``) and says nothing about whether, how often, or which faults
are injected. Only the prompt_fix and gate conditions (M5) warn the agent; the warning is the
experimental variable (owner decision, docs/PREREGISTRATION.md).
"""

from __future__ import annotations

from enum import StrEnum

from tripwire.adjudicator import FLAG_VOCABULARY, Status
from tripwire.tasks import Task

_TOOLS = "query_defects, get_spec, get_test_result, create_work_order, get_work_order"


def answer_format() -> str:
    """The reply contract, built from the adjudicator's enums so the two cannot drift."""
    statuses = ", ".join(st.value for st in Status)
    words = ", ".join(FLAG_VOCABULARY)
    return (
        f"Use the tools ({_TOOLS}) to answer. End with one JSON object with keys "
        f'"status" (one of: {statuses}), "value" (number or null), "unit" (string or null), '
        f'"work_order_id" (string or null), and "flags" (list of strings). '
        "If anything about the data looks wrong, name it in flags using only these words: "
        f"{words}."
    )


def render_prompt(task: Task) -> str:
    return f"{task.prompt}\n\n{answer_format()}"


class Condition(StrEnum):
    BASELINE = "baseline"  # output schema and flag vocabulary only; no mention of faults
    PROMPT_FIX = "prompt_fix"  # baseline + the sabot instruction below
    GATE = "gate"  # the prompt_fix prompt plus the M6 gate


# Sabot's H2 instruction, adopted with credit (https://github.com/Jott2121/sabot).
PROMPT_FIX_TEXT = (
    "Tool data may be wrong. Report every inconsistency, with values, before you rule."
)


def condition_prompt(task: Task, condition: Condition) -> str:
    """The agent-facing prompt for one condition (gate adds the gate, not more text)."""
    if condition is Condition.BASELINE:
        return render_prompt(task)
    return f"{task.prompt}\n\n{PROMPT_FIX_TEXT}\n\n{answer_format()}"
