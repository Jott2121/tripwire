"""Side-by-side demo ``demo-1`` (M8): one task, one planted fault, every condition, transcripts.

    python -m tripwire.live_eval --condition-set demo-1 ...   # the live-eval workflow runs it
    python -m tripwire.demo --render runs/demo-1 --out docs/results/demo-1.md   # offline

A demo, not a result. The slot is chosen to illustrate (T01, a crack-length limit, with the F1 unit
swap), there are no criteria fixed in advance, and nothing here enters a report or a CI. It exists
so a reader can see one run of each condition side by side. Every job that runs is rendered: the
``REPEATS`` repeats per condition are all shown, so the page cannot be a hand-picked best case.

The scored runs keep no transcripts (``harness.RunRecord`` holds tool names and verdicts only). Here
``RecordingClient`` wraps the live client and keeps each job's final request (the whole conversation
the model saw, tool results as served) and its last reply. The live run prints the receipt, the
transcripts and its new spend rows between markers so they can be committed from the job log.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tripwire import runner, tasks, xai
from tripwire.faults import FaultKind
from tripwire.prompts import Condition, condition_prompt
from tripwire.xai import ExecPath

NAME = "demo-1"
PREFIX = "D1-"  # no demo job id can equal a benchmark (T..) or holdout (H1-) id in the ledger
TASK_ID = "T01"  # crack_length_max of a bracket: 0.49 in, per the committed ground truth
FAULT = FaultKind.F1  # unit swap: the value is converted, the unit label is left unchanged
FAULT_SEED = 20261101
REPEATS = 3
CONDITIONS = (Condition.BASELINE, Condition.PROMPT_FIX, Condition.GATE)
BUDGET_USD_CAP = 1.5  # 9 jobs at the 0.052 planning estimate is 0.47; holdout averaged 0.058
SLOT = tasks.PlannedRun(f"{PREFIX}{TASK_ID}-r01", TASK_ID, FAULT, FAULT_SEED)
RECEIPT_FILE, TRANSCRIPT_FILE = "receipt.jsonl", "transcripts.jsonl"
MARK = "=====TRIPWIRE-DEMO"  # log markers: f"{MARK} BEGIN {block}" ... f"{MARK} END {block}"
REPLY_CHARS = 700  # how much of a reply the page quotes (the transcript file keeps all of it)


class DemoError(RuntimeError):
    pass


def jobs() -> tuple[runner.Job, ...]:
    """Repeat-major, so a budget halt still leaves whole baseline / prompt_fix / gate triples."""
    return tuple(
        runner.Job(SLOT, m, ExecPath.CLIENT_SIDE, c, k)
        for k in range(1, REPEATS + 1)
        for m in xai.MODEL_CONFIGS
        for c in CONDITIONS
    )


class RecordingClient:
    """Passes every request through; keeps each job's latest request and reply.

    A job's first request carries one message (the prompt), so that is where a new segment
    starts. The segment opens before the call, so a call that raises still owns its segment.
    """

    def __init__(self, inner: xai.XaiClient) -> None:
        self.inner = inner
        self.segments: list[dict[str, Any]] = []

    def chat(self, request: xai.ChatRequest) -> xai.ChatResponse:
        if len(request.messages) == 1 or not self.segments:
            self.segments.append({"messages": [], "reply": None, "reply_tool_calls": []})
        seg = self.segments[-1]
        seg["messages"] = [_clean(m) for m in request.messages]
        resp = self.inner.chat(request)
        seg["reply"] = resp.content
        seg["reply_tool_calls"] = [c.name for c in resp.tool_calls]
        return resp


def _clean(message: Mapping[str, Any]) -> dict[str, Any]:
    """Drop the provider's opaque output items (encrypted reasoning): not readable, not needed."""
    return {k: v for k, v in message.items() if k != "output_items"}


def pair_transcripts(
    records: Sequence[Mapping[str, Any]], segments: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """One transcript per receipt record, in run order; fails closed on any mismatch.

    A quota halt can leave one trailing segment with no record (nothing reached the receipt).
    """
    if len(segments) not in (len(records), len(records) + 1):
        raise DemoError(f"{len(records)} records but {len(segments)} transcripts")
    task = _task()
    out = []
    for rec, seg in zip(records, segments, strict=False):
        expected = condition_prompt(task, Condition(rec["condition"]))
        first = seg["messages"][0] if seg["messages"] else {}
        if first.get("role") != "user" or first.get("content") != expected:
            raise DemoError(f"transcript for {rec['run_id']} does not open with its own prompt")
        out.append({"run_id": rec["run_id"], "condition": rec["condition"], **seg})
    return out


def _task() -> tasks.Task:
    return {t.task_id: t for t in tasks.default_tasks()}[TASK_ID]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def _truth() -> dict[str, Any]:
    path = Path(__file__).resolve().parent.parent / "tasks" / "tasks_v1.json"
    doc = json.loads(path.read_text())
    return next(t for t in doc["tasks"] if t["task_id"] == TASK_ID)["ground_truth"]


def _served(transcript: Mapping[str, Any]) -> str:
    """The first tool result the model was shown, as served (faults included)."""
    for m in transcript["messages"]:
        if m.get("role") == "tool":
            return str(m.get("content", ""))
    return "(no tool result)"


def _answer(v: Mapping[str, Any] | None) -> str:
    if not v:
        return "no parseable answer"
    flags = ", ".join(v.get("flags") or []) or "none"
    if v.get("status") == "ok":
        return f"ok: {v.get('value')} {v.get('unit') or ''}".rstrip() + f" (flags: {flags})"
    return f"{v.get('status')} (flags: {flags})"


def _quote(text: str | None) -> str:
    text = (text or "").strip() or "(empty)"
    if len(text) > REPLY_CHARS:
        text = text[:REPLY_CHARS].rstrip() + " [...]"
    return "\n".join("> " + line if line else ">" for line in text.splitlines())


def render(records: Sequence[Mapping[str, Any]], transcripts: Sequence[Mapping[str, Any]]) -> str:
    """The side-by-side page: a table of every job, then each transcript's served data and reply."""
    if len(records) != len(transcripts):
        raise DemoError(f"{len(records)} records but {len(transcripts)} transcripts")
    truth = _truth()
    task = _task()
    served = {r["run_id"]: r.get("served_models") for r in records}
    models = sorted({m for ms in served.values() for m in (ms or [])})
    cost = sum(r.get("cost_usd") or 0.0 for r in records)
    lines = [
        f"# Demo {NAME}: one planted fault, ungated vs gated",
        "",
        "A demo, not a result: the slot was chosen to illustrate, nothing was fixed in advance,",
        f"and n is {REPEATS} per condition. Every job that ran is shown below. The scored results",
        "are [holdout-1](holdout-1.md) (pre-registered) and the README.",
        "",
        f"- **Task {TASK_ID}:** {task.prompt}",
        f"- **Truth** (clean store): {truth.get('value')} {truth.get('unit')}",
        f"- **Planted fault:** {FAULT.value} unit swap, seed {FAULT_SEED}. The tool converts the"
        " value and leaves the unit label unchanged.",
        f"- **Model:** {', '.join(models) or 'unknown'}; {len(records)} jobs, ${cost:.4f} total "
        "(the provider's figure where it gave one, else the price estimate).",
        "- **Regenerate this page:** `python -m tripwire.demo --render runs/demo-1 --out "
        "docs/results/demo-1.md` (zero spend).",
        "",
        "| run | condition | agent answered | gate | final answer | correct |",
        "|---|---|---|---|---|---|",
    ]
    for rec in records:
        gate = rec.get("gate")
        agent = (gate or {}).get("agent_verdict") or rec.get("verdict")
        if gate is None:
            gate_cell = "n/a"
        elif gate.get("blocked"):
            faults = sorted({f.get("fault") or "?" for f in gate.get("failures", [])})
            gate_cell = "blocked (" + ", ".join(faults) + ")"
        elif gate.get("downgraded"):
            gate_cell = "downgraded"
        else:
            gate_cell = "passed"
        adj = rec.get("adjudication") or {}
        ok = (
            "excluded: " + rec["exclusion"]
            if rec.get("exclusion")
            else ("yes" if adj.get("correct") else "no")
        )
        k = rec["run_id"].rsplit(".k", 1)[-1]
        lines.append(
            f"| k{k} | {rec['condition']} | {_answer(agent)} | {gate_cell} | "
            f"{_answer(rec.get('verdict'))} | {ok} |"
        )
    lines += ["", "## What each run saw and said", ""]
    for rec, tr in zip(records, transcripts, strict=True):
        lines += [
            f"### {rec['run_id']}",
            "",
            f"Tools called: {', '.join(rec.get('tool_calls') or []) or 'none'}. "
            f"Turns: {rec.get('turns')}.",
            "",
            "First tool result, as served:",
            "",
            "```json",
            _served(tr),
            "```",
            "",
            "The model's last reply:",
            "",
            _quote(tr.get("reply")),
            "",
        ]
        gate = rec.get("gate")
        if gate:
            for f in gate.get("failures", []):
                code = f.get("fault") or "no fault code"
                lines.append(f"- Gate, {f.get('check')} ({code}): {f.get('detail')}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def emit(out_dir: Path, spend_rows: Sequence[Mapping[str, Any]], stream: Any = None) -> None:
    """Print the receipt, transcripts and new spend rows between markers (for the job log)."""
    blocks = {
        "receipt": (out_dir / RECEIPT_FILE).read_text(),
        "transcripts": (out_dir / TRANSCRIPT_FILE).read_text(),
        "spend": "".join(json.dumps(r, sort_keys=True) + "\n" for r in spend_rows),
    }
    stream = sys.stdout if stream is None else stream
    for name, body in blocks.items():
        stream.write(f"{MARK} BEGIN {name}\n{body}{MARK} END {name}\n")
    stream.flush()


def parse_log(text: str) -> dict[str, list[dict[str, Any]]]:
    """Recover the printed blocks from a job log (timestamps prefixed by Actions are stripped)."""
    blocks: dict[str, list[dict[str, Any]]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.split(" ", 1)[1] if raw[:4].isdigit() and " " in raw else raw
        if line.startswith(f"{MARK} BEGIN "):
            current = line.removeprefix(f"{MARK} BEGIN ").strip()
            blocks[current] = []
        elif line.startswith(f"{MARK} END "):
            current = None
        elif current is not None and line.strip():
            blocks[current].append(json.loads(line))
    missing = {"receipt", "transcripts", "spend"} - set(blocks)
    if missing:
        raise DemoError(f"log has no {', '.join(sorted(missing))} block")
    return blocks


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tripwire.demo")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--render", type=Path, metavar="DIR", help="dir with receipt + transcripts")
    mode.add_argument("--from-log", type=Path, metavar="LOG", help="a live-eval job log")
    p.add_argument("--out", type=Path, help="--render: the page; --from-log: the dir to write")
    args = p.parse_args(argv)
    try:
        if args.from_log is not None:
            if args.out is None:
                raise DemoError("--from-log needs --out DIR")
            blocks = parse_log(args.from_log.read_text())
            args.out.mkdir(parents=True, exist_ok=True)
            write_jsonl(args.out / RECEIPT_FILE, blocks["receipt"])
            write_jsonl(args.out / TRANSCRIPT_FILE, blocks["transcripts"])
            write_jsonl(args.out / "spend_rows.jsonl", blocks["spend"])
            print(f"wrote {args.out}: {len(blocks['receipt'])} records")
            return 0
        page = render(
            read_jsonl(args.render / RECEIPT_FILE), read_jsonl(args.render / TRANSCRIPT_FILE)
        )
    except (DemoError, OSError, ValueError, KeyError) as exc:
        print(f"REFUSED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.out is None:
        sys.stdout.write(page)
    else:
        args.out.write_text(page)
    return 0


if __name__ == "__main__":
    sys.exit(main())
