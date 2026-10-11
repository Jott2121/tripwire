"""Agent-facing prompt text (M4): the task plus how to report, in the adjudicator's vocabulary.

The wording is deliberately neutral. It names the words a flag can use
(``adjudicator.FLAG_VOCABULARY``) and says nothing about whether, how often, or which faults
are injected; what the agent is told about that is a preregistration decision (M5).
"""

from __future__ import annotations

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
