"""Executable Codex CLI conformance gate for Ostiari's Responses endpoint.

The gate runs an exact supported Codex version against a loopback server that
uses Ostiari's real Responses request translator and typed SSE emitter. It
proves the pinned model profile omits unsupported fields, completes a safe
function-call round trip, reports OpenAI-shaped errors, and cancels an active
stream promptly.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ostiari_gateway.modules.llm_gateway.responses_proxy import ResponsesProxy
from starlette.requests import Request
from starlette.responses import JSONResponse

SUPPORTED_CODEX_VERSION = "0.148.0"
MODEL = "ostiari-codex"
TOKEN = "ostiari-codex-conformance-token"
SUCCESS_PROMPT = "OSTIARI_CONFORMANCE_SUCCESS"
ERROR_PROMPT = "OSTIARI_CONFORMANCE_ERROR"
CANCEL_PROMPT = "OSTIARI_CONFORMANCE_CANCEL"
FINAL_TEXT = "OSTIARI_CODEX_CONFORMANCE_OK"
ERROR_TEXT = "ostiari conformance error"


class _FakeChatProxy:
    """Return deterministic chat completions behind the real Responses proxy."""

    async def handle(self, request: Request) -> JSONResponse:
        body = await request.json()
        messages = body.get("messages", [])
        has_tool_output = any(
            isinstance(message, dict) and message.get("role") == "tool"
            for message in messages
        )
        if has_tool_output:
            message: dict[str, Any] = {"role": "assistant", "content": FINAL_TEXT}
            finish_reason = "stop"
        else:
            tool_name, arguments = _safe_tool_call(body.get("tools"))
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_ostiari_conformance",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(
                                arguments,
                                separators=(",", ":"),
                            ),
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        return JSONResponse(
            {
                "id": "chatcmpl_ostiari_conformance",
                "object": "chat.completion",
                "model": body.get("model", MODEL),
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        )


def _safe_tool_call(tools: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(tools, list):
        raise AssertionError("Codex request did not include function tools")
    functions: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        function = tool.get("function")
        if isinstance(function, dict):
            functions.append(function)
        else:
            functions.append(tool)
    preferred = next(
        (
            tool
            for tool in functions
            if tool.get("name") in {"shell_command", "exec_command"}
        ),
        None,
    )
    if preferred is None:
        raise AssertionError("Codex request did not expose a safe shell tool")

    name = str(preferred["name"])
    parameters = preferred.get("parameters")
    properties = (
        parameters.get("properties", {})
        if isinstance(parameters, dict)
        else {}
    )
    required = (
        parameters.get("required", [])
        if isinstance(parameters, dict)
        else []
    )
    command_key = "cmd" if name == "exec_command" else "command"
    arguments: dict[str, Any] = {}
    for key in required:
        schema = properties.get(key, {})
        if key == command_key:
            arguments[key] = _command_value(schema)
        else:
            arguments[key] = _minimal_value(schema)
    arguments.setdefault(command_key, _command_value(properties.get(command_key, {})))
    return name, arguments


def _command_value(schema: Any) -> str | list[str]:
    if isinstance(schema, dict) and schema.get("type") == "array":
        return ["/bin/sh", "-lc", "printf OSTIARI_TOOL_OK"]
    return "printf OSTIARI_TOOL_OK"


def _minimal_value(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return None
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    value_type = schema.get("type")
    if value_type == "string":
        return ""
    if value_type == "integer":
        return 1000
    if value_type == "number":
        return 1
    if value_type == "boolean":
        return False
    if value_type == "array":
        return []
    if value_type == "object":
        return {}
    return None


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.cancel_started = threading.Event()

    def record(self, body: dict[str, Any]) -> None:
        with self.lock:
            self.requests.append(body)

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.requests)


def _request_marker(body: dict[str, Any]) -> str:
    encoded = json.dumps(body.get("input"), ensure_ascii=False)
    for marker in (SUCCESS_PROMPT, ERROR_PROMPT, CANCEL_PROMPT):
        if marker in encoded:
            return marker
    return ""


def _make_handler(
    state: _State,
    proxy: ResponsesProxy,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            if self.path != "/v1/responses":
                self.send_error(404)
                return
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._json_error(401, "missing conformance bearer token")
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(content_length))
            except (TypeError, ValueError, json.JSONDecodeError):
                self._json_error(400, "malformed conformance request")
                return
            if not isinstance(body, dict):
                self._json_error(400, "request body must be an object")
                return
            state.record(body)

            marker = _request_marker(body)
            if marker == ERROR_PROMPT:
                self._json_error(400, ERROR_TEXT)
                return
            if marker == CANCEL_PROMPT:
                self._hold_stream(body)
                return
            if marker != SUCCESS_PROMPT:
                self._json_error(400, "missing conformance marker")
                return
            self._proxy_response(body)

        def _proxy_response(self, body: dict[str, Any]) -> None:
            import asyncio

            scope = {
                "type": "http",
                "method": "POST",
                "path": "/v1/responses",
                "headers": [],
                "query_string": b"",
                "server": ("127.0.0.1", self.server.server_port),
                "scheme": "http",
            }
            encoded = json.dumps(body).encode()
            sent = False

            async def receive() -> dict[str, Any]:
                nonlocal sent
                if sent:
                    return {"type": "http.disconnect"}
                sent = True
                return {
                    "type": "http.request",
                    "body": encoded,
                    "more_body": False,
                }

            request = Request(scope, receive)
            response = asyncio.run(proxy.handle(request))
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                if key.lower() not in {"content-length", "connection"}:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if hasattr(response, "body_iterator"):
                async def collect() -> bytes:
                    chunks: list[bytes] = []
                    async for chunk in response.body_iterator:
                        chunks.append(
                            chunk.encode() if isinstance(chunk, str) else chunk
                        )
                    return b"".join(chunks)

                self.wfile.write(asyncio.run(collect()))
            else:
                self.wfile.write(response.body)

        def _hold_stream(self, body: dict[str, Any]) -> None:
            response_id = "resp_ostiari_cancel"
            initial = {
                "id": response_id,
                "object": "response",
                "created_at": int(time.time()),
                "completed_at": None,
                "status": "in_progress",
                "error": None,
                "incomplete_details": None,
                "instructions": body.get("instructions"),
                "max_output_tokens": body.get("max_output_tokens"),
                "model": body.get("model", MODEL),
                "output": [],
                "parallel_tool_calls": True,
                "previous_response_id": None,
                "reasoning": None,
                "service_tier": "default",
                "store": False,
                "temperature": None,
                "text": {"format": {"type": "text"}},
                "tool_choice": "none",
                "tools": [],
                "top_p": None,
                "truncation": "disabled",
                "usage": None,
                "metadata": {},
            }
            events = [
                {
                    "type": "response.created",
                    "sequence_number": 0,
                    "response": initial,
                },
                {
                    "type": "response.in_progress",
                    "sequence_number": 1,
                    "response": initial,
                },
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for event in events:
                    payload = json.dumps(event, separators=(",", ":"))
                    self.wfile.write(
                        f"event: {event['type']}\ndata: {payload}\n\n".encode()
                    )
                self.wfile.flush()
                state.cancel_started.set()
                while True:
                    time.sleep(0.2)
                    self.wfile.write(b": waiting for cancellation\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

        def _json_error(self, status: int, message: str) -> None:
            payload = json.dumps(
                {
                    "error": {
                        "message": message,
                        "type": "invalid_request_error",
                        "code": "ostiari_conformance_error",
                    }
                },
                separators=(",", ":"),
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def _codex_command(
    *,
    codex_bin: Path,
    catalog: Path,
    workspace: Path,
    port: int,
    output: Path,
    prompt: str,
) -> list[str]:
    return [
        str(codex_bin),
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--cd",
        str(workspace),
        "--model",
        MODEL,
        "--output-last-message",
        str(output),
        "-c",
        f'model_catalog_json="{catalog}"',
        "-c",
        'model_provider="ostiari"',
        "-c",
        'model_providers.ostiari.name="Ostiari"',
        "-c",
        f'model_providers.ostiari.base_url="http://127.0.0.1:{port}/v1"',
        "-c",
        'model_providers.ostiari.env_key="OSTIARI_CODEX_TOKEN"',
        "-c",
        'model_providers.ostiari.wire_api="responses"',
        "-c",
        "model_providers.ostiari.request_max_retries=0",
        "-c",
        "model_providers.ostiari.stream_max_retries=0",
        "-c",
        "model_providers.ostiari.stream_idle_timeout_ms=5000",
        "-c",
        "model_providers.ostiari.requires_openai_auth=false",
        "-c",
        'approval_policy="never"',
        prompt,
    ]


def _environment(codex_home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "CODEX_HOME": str(codex_home),
            "OSTIARI_CODEX_TOKEN": TOKEN,
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    return environment


def _assert_request_contract(requests: list[dict[str, Any]]) -> None:
    success = [
        request for request in requests if _request_marker(request) == SUCCESS_PROMPT
    ]
    if len(success) < 2:
        raise AssertionError("Codex did not complete a function-call round trip")
    first = success[0]
    if first.get("model") != MODEL:
        raise AssertionError(f"unexpected model: {first.get('model')}")
    if first.get("stream") is not True or first.get("store") is not False:
        raise AssertionError("Codex did not request a stateless streamed response")
    for field in ("previous_response_id", "service_tier", "text"):
        if first.get(field) not in (None, {}):
            raise AssertionError(f"unsupported field sent by Codex: {field}")
    reasoning = first.get("reasoning")
    include = first.get("include")
    if reasoning not in (None, {}) and (
        not isinstance(reasoning, dict)
        or not reasoning
        or not set(reasoning) <= {"context", "effort", "summary"}
        or reasoning.get("context") not in (None, "all_turns")
        or any(reasoning.get(field) not in (None, "none") for field in ("effort", "summary"))
    ):
        raise AssertionError("Codex requested unsupported reasoning metadata")
    reasoning_context = reasoning.get("context") if isinstance(reasoning, dict) else None
    if reasoning_context == "all_turns":
        if include != ["reasoning.encrypted_content"]:
            raise AssertionError(
                "Codex omitted encrypted reasoning transport metadata"
            )
    elif include not in (None, []):
        raise AssertionError("Codex requested unsupported include fields")
    if not isinstance(first.get("tools"), list) or not first["tools"]:
        raise AssertionError("Codex request did not include tools")
    if not any(
        isinstance(item, dict) and item.get("type") == "function_call_output"
        for item in success[-1].get("input", [])
    ):
        raise AssertionError("Codex did not return the tool output")


def _request_diagnostics(requests: list[dict[str, Any]]) -> str:
    safe_fields = (
        "model",
        "reasoning",
        "include",
        "service_tier",
        "store",
        "stream",
    )
    sanitized = [
        {field: request.get(field) for field in safe_fields if field in request}
        for request in requests
    ]
    return json.dumps(sanitized, sort_keys=True, separators=(",", ":"))


def _run_success(
    command: list[str],
    *,
    environment: dict[str, str],
    output: Path,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "Codex success case failed:\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    if output.read_text().strip() != FINAL_TEXT:
        raise AssertionError("Codex did not consume Ostiari's final text event")
    return completed


def _run_error(
    command: list[str],
    *,
    environment: dict[str, str],
) -> None:
    completed = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    combined = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode == 0 or ERROR_TEXT not in combined:
        raise AssertionError(
            "Codex did not surface the OpenAI-shaped error:\n"
            f"{combined}"
        )


def _run_cancellation(
    command: list[str],
    *,
    environment: dict[str, str],
    state: _State,
) -> None:
    process = subprocess.Popen(
        command,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if not state.cancel_started.wait(timeout=20):
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        raise AssertionError(
            "Codex never entered the cancellation stream:\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )
    started = time.monotonic()
    process.send_signal(signal.SIGINT)
    try:
        stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate(timeout=5)
        raise AssertionError("Codex did not cancel the active stream") from exc
    if time.monotonic() - started >= 10:
        raise AssertionError("Codex cancellation exceeded the deadline")
    if FINAL_TEXT in f"{stdout}\n{stderr}":
        raise AssertionError("cancelled Codex request produced a final answer")


def _version(codex_bin: Path) -> str:
    completed = subprocess.run(
        [str(codex_bin), "--version"],
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    return completed.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--codex-bin",
        type=Path,
        default=Path(os.environ.get("OSTIARI_CODEX_BIN", "codex")),
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("config/codex/model-catalog.json"),
    )
    args = parser.parse_args()
    codex_bin = args.codex_bin
    catalog = args.catalog.resolve()
    version = _version(codex_bin)
    if version != f"codex-cli {SUPPORTED_CODEX_VERSION}":
        raise SystemExit(
            f"unsupported Codex CLI: {version!r}; "
            f"expected codex-cli {SUPPORTED_CODEX_VERSION}"
        )

    state = _State()
    proxy = ResponsesProxy(_FakeChatProxy())  # type: ignore[arg-type]
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state, proxy))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        with tempfile.TemporaryDirectory(prefix="ostiari-codex-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            codex_home = root / "codex-home"
            workspace.mkdir()
            codex_home.mkdir()
            environment = _environment(codex_home)

            success_output = root / "success.txt"
            try:
                _run_success(
                    _codex_command(
                        codex_bin=codex_bin,
                        catalog=catalog,
                        workspace=workspace,
                        port=server.server_port,
                        output=success_output,
                        prompt=(
                            f"{SUCCESS_PROMPT}: use one harmless shell tool, then "
                            "return the server-provided final answer."
                        ),
                    ),
                    environment=environment,
                    output=success_output,
                )
            except AssertionError as exc:
                raise AssertionError(
                    f"{exc}\nrequest metadata:\n"
                    f"{_request_diagnostics(state.snapshot())}"
                ) from exc
            _assert_request_contract(state.snapshot())

            _run_error(
                _codex_command(
                    codex_bin=codex_bin,
                    catalog=catalog,
                    workspace=workspace,
                    port=server.server_port,
                    output=root / "error.txt",
                    prompt=f"{ERROR_PROMPT}: verify error propagation.",
                ),
                environment=environment,
            )

            _run_cancellation(
                _codex_command(
                    codex_bin=codex_bin,
                    catalog=catalog,
                    workspace=workspace,
                    port=server.server_port,
                    output=root / "cancel.txt",
                    prompt=f"{CANCEL_PROMPT}: keep this stream open.",
                ),
                environment=environment,
                state=state,
            )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    print(
        "Codex 0.148.0 request, tool-loop, streaming, error, and "
        "cancellation conformance passed"
    )


if __name__ == "__main__":
    main()
