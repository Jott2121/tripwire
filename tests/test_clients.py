"""v2 provider clients over recorded HTTP only: no network, no real key, no spend."""

from __future__ import annotations

import json
from datetime import date

import pytest
import support as sp

from tripwire import clients as cl
from tripwire import tasks as tk
from tripwire import xai
from tripwire.harness import run_one
from tripwire.prompts import Condition
from tripwire.xai import ChatRequest, ExecPath, ToolCallRequest

TOOLS = ({"type": "function", "function": {"name": "t", "description": "d", "parameters": {}}},)


class Fake:
    """A recorded transport: replays bodies in order and keeps what was sent."""

    def __init__(self, *replies):
        self.replies, self.sent = list(replies), []

    def __call__(self, url, headers, data, timeout):
        self.sent.append((url, dict(headers), json.loads(data)))
        status, body = self.replies.pop(0)
        return status, body if isinstance(body, bytes) else json.dumps(body).encode()


def req(*messages):
    return ChatRequest("m-id", "medium", messages or ({"role": "user", "content": "q"},), TOOLS)


# --- Together (OpenAI-compatible) ---------------------------------------------------------------
CC_TOOL_TURN = {
    "model": "openai/gpt-oss-120b",
    "choices": [
        {
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "reasoning": "need the spec",
                "tool_calls": [
                    {
                        "id": "k1",
                        "type": "function",
                        "index": 0,
                        "function": {"name": "get_spec", "arguments": '{"a": 1}'},
                    },
                ],  # fmt: skip
            },
        }
    ],
    "usage": {"prompt_tokens": 900, "completion_tokens": 40, "total_tokens": 940},
}
CC_FINAL = {
    "model": "openai/gpt-oss-120b",
    "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}],
    "usage": {"prompt_tokens": 1000, "completion_tokens": 10},
}


def test_chat_completions_round_trip_echoes_reasoning_and_sends_tool_results():
    fake = Fake((200, CC_TOOL_TURN), (200, CC_FINAL))
    client = cl.ChatCompletionsClient("tg-key", fake)
    first = client.chat(req())
    assert first.tool_calls == (ToolCallRequest("k1", "get_spec", {"a": 1}),)
    assert (first.served_model, first.input_tokens, first.output_tokens) == (
        "openai/gpt-oss-120b", 900, 40,
    )  # fmt: skip
    history = (
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "output_items": list(first.output_items)},
        {"role": "tool", "tool_call_id": "k1", "content": '{"ok": true}'},
    )
    final = client.chat(req(*history))
    assert final.content == "done" and final.tool_calls == ()
    url, headers, body = fake.sent[1]
    assert url == cl.TOGETHER_URL and headers["Authorization"] == "Bearer tg-key"
    assert body["reasoning_effort"] == "medium" and body["max_tokens"] == cl.MAX_OUTPUT_TOKENS
    assert body["tools"] == [dict(t) for t in TOOLS]
    echoed = body["messages"][1]
    assert echoed["reasoning"] == "need the spec" and "content" not in echoed  # None dropped
    assert echoed["tool_calls"][0]["id"] == "k1"
    assert body["messages"][2] == {"role": "tool", "tool_call_id": "k1", "content": '{"ok": true}'}


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"usage": None}, xai.UsageUnrecognised),
        ({"usage": {"prompt_tokens": 1}}, xai.UsageUnrecognised),
        ({"model": ""}, xai.XaiError),
        ({"choices": []}, xai.XaiError),
        ({"error": {"message": "x"}}, xai.XaiError),
    ],
)
def test_chat_completions_parse_is_strict(change, error):
    with pytest.raises(error):
        cl.parse_chat_completion(CC_FINAL | change)


@pytest.mark.parametrize(
    ("finish", "message"),
    [
        ("length", {"role": "assistant", "content": "cut"}),
        ("function_call", {"role": "assistant", "content": ""}),
        ("stop", {"role": "assistant", "content": 3}),
        ("tool_calls", {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]}),
        ("tool_calls", {"role": "assistant", "content": "", "tool_calls": [
            {"id": "x", "type": "function", "function": {"name": "n", "arguments": "[1]"}}]}),
        ("tool_calls", {"role": "assistant", "content": "", "tool_calls": [
            {"id": "x", "type": "function", "function": {"name": "n", "arguments": "{bad"}}]}),
    ],
)  # fmt: skip
def test_chat_completions_refuses_truncation_and_malformed_calls(finish, message):
    body = CC_FINAL | {"choices": [{"finish_reason": finish, "message": message}]}
    with pytest.raises(xai.XaiError):
        cl.parse_chat_completion(body)


# --- Anthropic Messages ------------------------------------------------------------------------
AN_TOOL_TURN = {
    "type": "message",
    "model": "claude-sonnet-5-5",
    "stop_reason": "tool_use",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "tu1", "name": "get_spec", "input": {"a": 1}},
        {"type": "tool_use", "id": "tu2", "name": "query_defects", "input": {}},
    ],
    "usage": {"input_tokens": 50, "output_tokens": 30, "cache_creation_input_tokens": 900,
              "cache_read_input_tokens": 0},
}  # fmt: skip
AN_FINAL = {
    "type": "message",
    "model": "claude-sonnet-5-5",
    "stop_reason": "end_turn",
    "content": [{"type": "text", "text": "done"}],
    "usage": {"input_tokens": 40, "output_tokens": 8, "cache_creation_input_tokens": 100,
              "cache_read_input_tokens": 900},
}  # fmt: skip


def test_anthropic_round_trip_echoes_blocks_and_merges_tool_results():
    fake = Fake((200, AN_TOOL_TURN), (200, AN_FINAL))
    client = cl.AnthropicClient("an-key", fake)
    first = client.chat(req())
    assert [c.call_id for c in first.tool_calls] == ["tu1", "tu2"]
    counts = (first.input_tokens, first.cache_write_tokens, first.cached_input_tokens)
    assert counts == (950, 900, 0)
    assert first.content == "Checking."
    history = (
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "Checking.", "output_items": list(first.output_items)},
        {"role": "tool", "tool_call_id": "tu1", "content": "{}"},
        {"role": "tool", "tool_call_id": "tu2", "content": "[]"},
    )
    final = client.chat(req(*history))
    assert (final.content, final.input_tokens, final.cached_input_tokens) == ("done", 1040, 900)
    url, headers, body = fake.sent[1]
    assert url == cl.ANTHROPIC_URL
    assert headers["x-api-key"] == "an-key" and headers["anthropic-version"] == "2026-10-21"
    assert body["output_config"] == {"effort": "medium"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert "fallbacks" not in body  # another model must never answer for the one under test
    assert body["tools"] == [{"name": "t", "description": "d", "input_schema": {}}]
    assert body["messages"][1] == {"role": "assistant", "content": AN_TOOL_TURN["content"]}
    assert body["messages"][2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu1", "content": "{}"},
        {"type": "tool_result", "tool_use_id": "tu2", "content": "[]"},
    ]}  # fmt: skip
    assert len(body["messages"]) == 3


def test_anthropic_refusal_is_a_final_reply_with_no_calls_run():
    body = AN_TOOL_TURN | {"stop_reason": "refusal"}
    resp = cl.parse_anthropic(body)
    assert resp.refused and resp.tool_calls == ()


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"stop_reason": "max_tokens"}, xai.XaiError),
        ({"stop_reason": "pause_turn"}, xai.XaiError),
        ({"content": [{"type": "server_tool_use"}]}, xai.XaiError),
        ({"content": [{"type": "tool_use", "id": "x"}]}, xai.XaiError),
        ({"stop_reason": "tool_use"}, xai.XaiError),  # tool_use with no tool_use block
        ({"type": "error"}, xai.XaiError),
        ({"usage": {"input_tokens": 1, "output_tokens": 1}}, xai.UsageUnrecognised),
        ({"usage": None}, xai.UsageUnrecognised),
        ({"model": None}, xai.XaiError),
    ],
)
def test_anthropic_parse_is_strict(change, error):
    with pytest.raises(error):
        cl.parse_anthropic(AN_FINAL | change)


def test_anthropic_assistant_turn_needs_its_blocks():
    msgs = ({"role": "user", "content": "q"}, {"role": "assistant", "content": "x"})
    with pytest.raises(xai.XaiError):
        cl.AnthropicClient("k", Fake()).chat(req(*msgs))


# --- shared behaviour ---------------------------------------------------------------------------
@pytest.mark.parametrize("make", [cl.ChatCompletionsClient, cl.AnthropicClient])
@pytest.mark.parametrize(
    ("status", "error"), [(429, xai.QuotaExhausted), (402, xai.QuotaExhausted),
                          (529, xai.QuotaExhausted), (500, xai.XaiError), (400, xai.XaiError)],
)  # fmt: skip
def test_http_status_handling(make, status, error):
    with pytest.raises(error):
        make("k", Fake((status, b"secret detail"))).chat(req())


@pytest.mark.parametrize("make", [cl.ChatCompletionsClient, cl.AnthropicClient])
@pytest.mark.parametrize("raw", [b"not json", b"[1]"])
def test_non_object_bodies_raise(make, raw):
    with pytest.raises(xai.XaiError):
        make("k", Fake((200, raw))).chat(req())


@pytest.mark.parametrize("make", [cl.ChatCompletionsClient, cl.AnthropicClient])
def test_keys_are_required_hidden_and_remote_mcp_refused(make):
    with pytest.raises(xai.LiveCallsDisabled):
        make("", Fake())
    client = make("sk-secret", Fake())
    assert "sk-secret" not in repr(client)
    remote = ChatRequest("m", None, ({"role": "user", "content": "q"},),
                         remote_mcp=xai.RemoteMcp("u", "E"))  # fmt: skip
    with pytest.raises(xai.LiveCallsDisabled):
        client.chat(remote)


@pytest.mark.parametrize("make", [cl.ChatCompletionsClient, cl.AnthropicClient])
def test_unknown_message_roles_raise(make):
    with pytest.raises(xai.XaiError):
        make("k", Fake()).chat(req({"role": "system", "content": "x"}))


def test_no_effort_sends_no_effort_field():
    fake_cc, fake_an = Fake((200, CC_FINAL)), Fake((200, AN_FINAL))
    plain = ChatRequest("m", None, ({"role": "user", "content": "q"},), TOOLS)
    cl.ChatCompletionsClient("k", fake_cc).chat(plain)
    cl.AnthropicClient("k", fake_an).chat(plain)
    assert "reasoning_effort" not in fake_cc.sent[0][2]
    assert "output_config" not in fake_an.sent[0][2]


# --- prices, rows and the factory ---------------------------------------------------------------
def test_cost_prices_cache_reads_and_writes():
    p = cl.SONNET_5_5_PRICING
    # 1000 input = 100 plain + 600 read + 300 written; 100 output
    want = (100 * 2.00 + 600 * 0.10 + 300 * 2.50 + 100 * 10.00) / 1e6
    assert p.cost(1000, 100, 600, 300) == pytest.approx(want)
    assert p.cost(1000, 100) == pytest.approx((1000 * 2.00 + 100 * 10.00) / 1e6)


def test_cost_without_a_cached_rate_bills_reads_as_input_and_refuses_unknown_writes():
    p = cl.GPT_OSS_120B_PRICING
    assert p.cost(1000, 0, 400) == pytest.approx(1000 * 0.15 / 1e6)
    with pytest.raises(ValueError):
        p.cost(1000, 0, 0, 10)
    with pytest.raises(ValueError):
        p.cost(10, 0, 8, 8)
    with pytest.raises(ValueError):
        p.cost(-1, 0)


def test_long_context_tier_bills_cached_reads_at_the_long_input_rate():
    p = xai.GROK_4_7_PRICING
    assert p.cost(300_000, 0, 100_000) == pytest.approx(300_000 * 4.00 / 1e6)


def test_gemini_rate_is_dated():
    p = cl.GEMINI_3_8_FLASH_PRICING
    assert p.in_force(date(2026, 12, 31)) and not p.in_force(date(2027, 1, 1))
    assert cl.SONNET_5_5_PRICING.in_force(date(2030, 1, 1))


def test_v2_rows_match_the_preregistration():
    rows = {r.slot: r for r in cl.V2_MODELS}
    assert {s: (r.stage, r.provider, r.config.model_id) for s, r in rows.items()} == {
        "M1": (1, "xai", "grok-4.7"),
        "M4": (1, "together", "openai/gpt-oss-120b"),
        "M2": (2, "anthropic", "claude-sonnet-5-5"),
        "M3": (2, "google", "gemini-3.8-flash"),
    }
    assert all(r.config.effort == "medium" and r.config.resolved for r in rows.values())
    assert len({r.key_env for r in rows.values()}) == 4
    assert cl.v2_row("gpt-oss-120b").slot == "M4"
    with pytest.raises(KeyError):
        cl.v2_row("nope")


def test_make_client_builds_each_confirmed_provider_and_refuses_gemini():
    fake = Fake()
    kinds = {r.provider: type(cl.make_client(r, "k", fake)) for r in cl.V2_MODELS[:3]}
    assert kinds == {"xai": xai.LiveClient, "together": cl.ChatCompletionsClient,
                     "anthropic": cl.AnthropicClient}  # fmt: skip
    with pytest.raises(xai.LiveCallsDisabled):
        cl.make_client(cl.v2_row("gemini-3.8-flash"), "k", fake)


# --- end to end through the real harness ---------------------------------------------------------
def _an_turn(calls, text="", stop="tool_use", usage=(50, 30, 0, 0)):
    blocks = [{"type": "thinking", "thinking": "", "signature": "s"}]
    blocks += [{"type": "text", "text": text}] if text else []
    blocks += [{"type": "tool_use", "id": c.call_id, "name": c.name, "input": dict(c.args)}
               for c in calls]  # fmt: skip
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens")  # fmt: skip
    return {"type": "message", "model": "claude-sonnet-5-5", "stop_reason": stop,
            "content": blocks, "usage": dict(zip(keys, usage, strict=True))}  # fmt: skip


def test_a_run_through_the_anthropic_client_scores_and_prices_cache_tokens():
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    fake = Fake(
        (200, _an_turn(sp.read_calls(task), usage=(20, 30, 1000, 0))),
        (200, _an_turn((), sp.truth_json(task), "end_turn", (20, 10, 200, 1000))),
    )
    row = cl.v2_row("claude-sonnet-5-5")
    rec = run_one(
        sp.plan(task, None), task, row.config, ExecPath.CLIENT_SIDE, Condition.BASELINE,
        cl.make_client(row, "k", fake), run_id="an-1", clock=sp.CLOCK, pricing=row.pricing,
    )  # fmt: skip
    assert rec.adjudication["correct"] and rec.error is None and not rec.refused
    assert rec.served_models == ("claude-sonnet-5-5",) * 2
    assert (rec.input_tokens, rec.cached_input_tokens, rec.cache_write_tokens) == (2240, 1000, 1200)
    want = (40 * 2.00 + 1000 * 0.10 + 1200 * 2.50 + 40 * 10.00) / 1e6
    assert rec.cost_usd == pytest.approx(want) and rec.cost_in_usd_ticks is None
    second = fake.sent[1][2]["messages"]
    assert second[1]["content"][0]["type"] == "thinking"  # echoed unchanged
    assert {b["type"] for b in second[2]["content"]} == {"tool_result"}


def test_a_refused_run_is_scored_incorrect_not_excluded():
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    fake = Fake((200, _an_turn((), "I can't help with that.", "refusal")))
    row = cl.v2_row("claude-sonnet-5-5")
    rec = run_one(
        sp.plan(task, None), task, row.config, ExecPath.CLIENT_SIDE, Condition.BASELINE,
        cl.make_client(row, "k", fake), run_id="an-2", clock=sp.CLOCK, pricing=row.pricing,
    )  # fmt: skip
    assert rec.refused and rec.exclusion is None and not rec.adjudication["correct"]


def test_chat_completions_rebuilds_an_assistant_turn_without_provider_items():
    fake = Fake((200, CC_FINAL))
    wire = {"id": "k1", "type": "function", "function": {"name": "n", "arguments": "{}"}}
    history = ({"role": "user", "content": "q"},
               {"role": "assistant", "content": None, "tool_calls": [wire]},
               {"role": "tool", "tool_call_id": "k1", "content": "{}"})  # fmt: skip
    cl.ChatCompletionsClient("k", fake).chat(req(*history))
    assert fake.sent[0][2]["messages"][1] == {
        "role": "assistant", "content": "", "tool_calls": [wire],
    }  # fmt: skip


@pytest.mark.parametrize(
    "choice",
    [
        {"finish_reason": "stop"},
        "not-a-dict",
        {"finish_reason": "tool_calls", "message": {"content": "", "tool_calls": [
            {"type": "function", "function": {"name": "n", "arguments": "{}"}}]}},
    ],
)  # fmt: skip
def test_chat_completions_refuses_a_choice_without_a_usable_message(choice):
    with pytest.raises(xai.XaiError):
        cl.parse_chat_completion(CC_FINAL | {"choices": [choice]})
