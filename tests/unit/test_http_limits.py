"""Unit tests for the shared ASGI DoS guards (ostiari.http_limits).

gateway/tests/test_dos_limits.py covers these through a real gateway app, which
proves the wiring. These drive the middleware directly, which is the only way to
reach the paths a TestClient can't produce: a mid-body ``http.disconnect``, a
non-http scope, a body with no Content-Length, and the receive-channel replay
that keeps streaming responses alive.
"""

from __future__ import annotations

import json

import pytest

from ostiari.http_limits import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    max_body_bytes,
    rate_limit_rpm,
)


async def echo_app(scope, receive, send):
    """Reads the whole request body, replies with what it received."""
    body = b""
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            body = b"<disconnect>"
            break
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


async def drive(
    mw,
    messages,
    *,
    headers=(),
    scope_type="http",
    client=("1.2.3.4", 0),
    state=None,
):
    """Run one request through `mw`, returning the ASGI messages it sent."""
    sent: list[dict] = []
    pending = iter(messages)

    async def receive():
        return next(pending)

    async def send(message):
        sent.append(message)

    await mw(
        {
            "type": scope_type,
            "headers": list(headers),
            "client": client,
            "state": state or {},
        },
        receive,
        send,
    )
    return sent


def body(content: bytes = b"", *, more=False):
    return {"type": "http.request", "body": content, "more_body": more}


ONE_REQUEST = [body()]


class TestBodySizeLimit:
    @pytest.mark.asyncio
    async def test_chunked_body_under_limit_passes_through_intact(self):
        mw = BodySizeLimitMiddleware(echo_app, max_bytes=100)
        sent = await drive(mw, [body(b"a" * 50, more=True), body(b"b" * 40)])
        assert sent[0]["status"] == 200
        assert sent[1]["body"] == b"a" * 50 + b"b" * 40

    @pytest.mark.asyncio
    async def test_declared_content_length_is_rejected_before_reading_body(self):
        """No receive() is queued: an oversized Content-Length must 413 without
        the middleware ever pulling the body, which is the point of the guard."""
        mw = BodySizeLimitMiddleware(echo_app, max_bytes=100)
        sent = await drive(mw, [], headers=[(b"content-length", b"999")])
        assert sent[0]["status"] == 413
        assert "exceeds limit (100 bytes)" in json.loads(sent[1]["body"])["detail"]

    @pytest.mark.asyncio
    async def test_oversized_body_without_content_length_still_rejected(self):
        """Chunked / length-less uploads are the case a header check misses."""
        mw = BodySizeLimitMiddleware(echo_app, max_bytes=100)
        sent = await drive(mw, [body(b"x" * 150)])
        assert sent[0]["status"] == 413

    @pytest.mark.asyncio
    async def test_limit_trips_mid_stream_before_the_whole_body_is_buffered(self):
        """The cap must be enforced as chunks arrive — otherwise an attacker
        streams unlimited bytes into memory and the check comes too late."""
        mw = BodySizeLimitMiddleware(echo_app, max_bytes=100)
        chunks = [body(b"x" * 60, more=True), body(b"x" * 60, more=True),
                  body(b"never read")]
        pending = iter(chunks)
        consumed = 0

        async def receive():
            nonlocal consumed
            consumed += 1
            return next(pending)

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        await mw({"type": "http", "headers": [], "client": ("1.2.3.4", 0)}, receive, send)
        assert sent[0]["status"] == 413
        assert consumed == 2  # stopped at the chunk that crossed the cap

    @pytest.mark.asyncio
    async def test_client_disconnect_mid_body_is_handed_downstream(self):
        mw = BodySizeLimitMiddleware(echo_app, max_bytes=100)
        sent = await drive(mw, [body(b"x", more=True), {"type": "http.disconnect"}])
        assert sent[0]["status"] == 200
        assert sent[1]["body"] == b"<disconnect>"

    @pytest.mark.asyncio
    async def test_body_replays_once_then_defers_to_the_real_receive_channel(self):
        """The regression this guards: returning a synthetic disconnect after the
        replay makes a StreamingResponse think the client vanished and abort the
        stream, so SSE endpoints silently truncate."""
        seen: list[str] = []

        async def two_reads(scope, receive, send):
            first = await receive()
            seen.append(first["body"].decode())
            second = await receive()
            seen.append(second["type"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        mw = BodySizeLimitMiddleware(two_reads, max_bytes=100)
        await drive(mw, [body(b"hi"), {"type": "http.disconnect"}])
        assert seen == ["hi", "http.disconnect"]

    @pytest.mark.asyncio
    async def test_non_http_scope_bypasses_the_cap_entirely(self):
        """Websocket/lifespan scopes have no request body to limit; buffering
        them would break the connection."""
        mw = BodySizeLimitMiddleware(echo_app, max_bytes=100)
        sent = await drive(mw, [body(b"z" * 500)], scope_type="websocket")
        assert sent[0]["status"] == 200
        assert len(sent[1]["body"]) == 500

    def test_default_limit_and_env_override(self, monkeypatch):
        monkeypatch.delenv("OSTIARI_MAX_BODY_BYTES", raising=False)
        assert max_body_bytes() == 10 * 1024 * 1024
        monkeypatch.setenv("OSTIARI_MAX_BODY_BYTES", "4096")
        assert max_body_bytes() == 4096
        # Garbage and non-positive values fall back rather than disabling the cap.
        for bad in ("0", "-1", "lots", ""):
            monkeypatch.setenv("OSTIARI_MAX_BODY_BYTES", bad)
            assert max_body_bytes() == 10 * 1024 * 1024


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_allows_up_to_the_limit_then_429s(self):
        mw = RateLimitMiddleware(echo_app, rpm=2)
        codes = [(await drive(mw, ONE_REQUEST))[0]["status"] for _ in range(4)]
        assert codes == [200, 200, 429, 429]

    @pytest.mark.asyncio
    async def test_429_carries_retry_after(self):
        mw = RateLimitMiddleware(echo_app, rpm=1)
        await drive(mw, ONE_REQUEST)
        sent = await drive(mw, ONE_REQUEST)
        assert sent[0]["status"] == 429
        assert dict(sent[0]["headers"])[b"retry-after"] == b"60"

    @pytest.mark.asyncio
    async def test_keyed_by_agent_id_then_client_ip(self):
        mw = RateLimitMiddleware(echo_app, rpm=1)
        await drive(mw, ONE_REQUEST, headers=[(b"x-agent-id", b"A")])
        # A is exhausted; B has its own budget, as does a different IP.
        assert (await drive(mw, ONE_REQUEST, headers=[(b"x-agent-id", b"A")]))[0]["status"] == 429
        assert (await drive(mw, ONE_REQUEST, headers=[(b"x-agent-id", b"B")]))[0]["status"] == 200
        assert (await drive(mw, ONE_REQUEST, client=("9.9.9.9", 0)))[0]["status"] == 200

    @pytest.mark.asyncio
    async def test_verified_agent_identity_overrides_caller_header(self):
        mw = RateLimitMiddleware(echo_app, rpm=1)
        assert (
            await drive(
                mw,
                ONE_REQUEST,
                headers=[(b"x-agent-id", b"spoof-a")],
                state={"agent_id": "verified"},
            )
        )[0]["status"] == 200
        assert (
            await drive(
                mw,
                ONE_REQUEST,
                headers=[(b"x-agent-id", b"spoof-b")],
                state={"agent_id": "verified"},
            )
        )[0]["status"] == 429

    @pytest.mark.asyncio
    async def test_window_slides_so_the_budget_recovers(self, monkeypatch):
        """A sliding window, not a permanent ban: once the old hits age past 60s
        the caller gets its full budget back. Clock is faked because the real
        thing would need the test to wait a minute."""
        clock = [1_000.0]
        monkeypatch.setattr("ostiari.http_limits.time.monotonic", lambda: clock[0])
        mw = RateLimitMiddleware(echo_app, rpm=2)
        codes = [(await drive(mw, ONE_REQUEST))[0]["status"] for _ in range(3)]
        assert codes == [200, 200, 429]
        clock[0] += 61.0
        assert (await drive(mw, ONE_REQUEST))[0]["status"] == 200

    @pytest.mark.asyncio
    async def test_disabled_when_rpm_is_zero(self):
        mw = RateLimitMiddleware(echo_app, rpm=0)
        codes = [(await drive(mw, ONE_REQUEST))[0]["status"] for _ in range(5)]
        assert codes == [200] * 5

    @pytest.mark.asyncio
    async def test_shared_store_decides_and_receives_the_window(self):
        """With a store the limit is fleet-wide, so the verdict must come from
        it and not from the per-process deque."""
        calls: list[tuple[str, int, float]] = []

        class Store:
            def rate_allow(self, key, limit, window_s):
                calls.append((key, limit, window_s))
                return len(calls) <= 1

        mw = RateLimitMiddleware(echo_app, rpm=5, store=Store())
        codes = [(await drive(mw, ONE_REQUEST, headers=[(b"x-agent-id", b"A")]))[0]["status"]
                 for _ in range(2)]
        assert codes == [200, 429]  # store said no, despite rpm=5 and 2 requests
        assert calls[0] == ("A", 5, 60.0)

    @pytest.mark.asyncio
    async def test_non_http_scope_is_not_rate_limited(self):
        mw = RateLimitMiddleware(echo_app, rpm=1)
        for _ in range(3):
            sent = await drive(mw, ONE_REQUEST, scope_type="websocket")
            assert sent[0]["status"] == 200

    def test_rpm_from_env_defaults_to_disabled(self, monkeypatch):
        monkeypatch.delenv("OSTIARI_GATEWAY_RATE_LIMIT_RPM", raising=False)
        assert rate_limit_rpm() == 0
        monkeypatch.setenv("OSTIARI_GATEWAY_RATE_LIMIT_RPM", "30")
        assert rate_limit_rpm() == 30
        monkeypatch.setenv("OSTIARI_GATEWAY_RATE_LIMIT_RPM", "nonsense")
        assert rate_limit_rpm() == 0
