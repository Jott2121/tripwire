"""MCP server (M2): the five Tripwire tools over a :class:`Store`, Streamable HTTP, bearer auth.

    TRIPWIRE_SERVER_TOKEN=... python -m tripwire.server --seed 1

Fails closed: the server refuses to start without a token in the environment, every
HTTP request needs ``Authorization: Bearer <token>`` (constant-time compare), a miss is a
typed ``NOT_FOUND`` tool error and bad input a typed ``VALIDATION`` one, never an empty
success. Results are serialized with ``contract.to_jsonable`` so units, revision and
provenance travel as structured fields. The token is never logged or echoed.
"""

from __future__ import annotations

import argparse
import functools
import hmac
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from starlette.types import ASGIApp, Receive, Scope, Send

from tripwire import contract as c
from tripwire import synthdata
from tripwire.store import NotFound, Store, StoreError

TOKEN_ENV = "TRIPWIRE_SERVER_TOKEN"
TOOLS = ("query_defects", "get_spec", "get_test_result", "create_work_order", "get_work_order")
Clock = Callable[[], datetime]


class ServerConfigError(RuntimeError):
    """The server cannot start safely (for example, no token configured)."""


def _plain(obj: Any) -> Any:
    """``contract.to_jsonable`` for a result envelope (a dict of contract records)."""
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    return c.to_jsonable(obj)


def _guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Map store/contract failures to typed tool errors; nothing fails open."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return _plain(fn(*args, **kwargs))
        except NotFound as exc:
            raise ToolError(f"NOT_FOUND: {exc.args[0]}") from exc
        except (StoreError, c.ContractError, ValueError) as exc:
            raise ToolError(f"VALIDATION: {exc}") from exc

    return wrapper


def _parse_since(since: str | None) -> date | None:
    if since is None:
        return None
    try:
        return date.fromisoformat(since)
    except ValueError as exc:
        raise StoreError(f"since must be an ISO date (YYYY-MM-DD), got {since!r}") from exc


def build_mcp(store: Store, clock: Clock | None = None) -> FastMCP:
    """The FastMCP app exposing exactly the five tools over ``store``."""
    now = clock or (lambda: datetime.now(UTC))
    mcp = FastMCP("tripwire", stateless_http=True, json_response=True)

    def query_defects(
        jasc_code: str | None = None,
        part_number: str | None = None,
        registry: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Defects matching all filters (since = ISO date), plus total and truncated."""
        page = store.query_defects(
            jasc_code=jasc_code,
            part_number=part_number,
            registry=registry,
            since=_parse_since(since),
            limit=limit,
        )
        return {"defects": page.defects, "total": page.total, "truncated": page.truncated}

    def get_spec(
        spec_id: str, parameter: str, applies_to: str, revision: str | None = None
    ) -> dict[str, Any]:
        """The current revision of one spec row, or exactly ``revision`` when given."""
        return {"spec": store.get_spec(spec_id, parameter, applies_to, revision)}

    def get_test_result(test_id: str) -> dict[str, Any]:
        """One test result by id."""
        return {"test_result": store.get_test_result(test_id)}

    def create_work_order(defect_id: str, action: str, created_by: str) -> dict[str, Any]:
        """Create an OPEN work order against a defect; returns the stored record."""
        return {"work_order": store.create_work_order(defect_id, action, created_by, now())}

    def get_work_order(work_order_id: str) -> dict[str, Any]:
        """One work order by id."""
        return {"work_order": store.get_work_order(work_order_id)}

    tools = (query_defects, get_spec, get_test_result, create_work_order, get_work_order)
    for fn in tools:
        mcp.tool(name=fn.__name__)(_guarded(fn))
    for tool in mcp._tool_manager.list_tools():  # unknown fields are errors, not ignored
        tool.parameters["additionalProperties"] = False
        model = tool.fn_metadata.arg_model
        model.model_config["extra"] = "forbid"
        model.model_rebuild(force=True)
    return mcp


class BearerAuth:
    """ASGI middleware: deny every request without the exact bearer token (fail closed)."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        if not isinstance(token, str) or not token:
            raise ServerConfigError(f"{TOKEN_ENV} is not set; refusing to start without auth")
        self._app = app
        self._token = token.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http" or not self._allowed(scope):
            await self._deny(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _allowed(self, scope: Scope) -> bool:
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                scheme, _, presented = value.partition(b" ")
                ok_scheme = hmac.compare_digest(scheme.lower(), b"bearer")
                return hmac.compare_digest(presented, self._token) and ok_scheme
        return False

    @staticmethod
    async def _deny(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        body = json.dumps({"error": "UNAUTHORIZED"}).encode()
        headers = [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]
        await send({"type": "http.response.start", "status": 401, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def build_app(
    store: Store, token: str | None, clock: Clock | None = None
) -> tuple[ASGIApp, FastMCP]:
    """The authenticated ASGI app and its FastMCP; a missing token raises ServerConfigError."""
    mcp = build_mcp(store, clock)
    return BearerAuth(mcp.streamable_http_app(), token or ""), mcp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    store = Store.from_dataset(synthdata.generate(args.seed))
    try:
        app, _ = build_app(store, os.environ.get(TOKEN_ENV))
    except ServerConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    import uvicorn  # pragma: no cover

    uvicorn.run(app, host=args.host, port=args.port)  # pragma: no cover
    return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
