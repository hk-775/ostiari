"""Run the packaged AxonLLM demo without repository-local shell scripts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser


_MODELS = (
    "claude-sonnet",
    "grok-3-mini",
    "groq-llama-3.3-70b",
    "groq-llama-3.1-8b",
    "together-llama-3.3-70b",
    "together-deepseek-r1",
    "fireworks-deepseek-v4",
)
_PROMPTS = (
    "What is the capital of France? Answer in one sentence.",
    "Explain recursion in one sentence.",
    "What year did the internet become publicly available?",
    "Name three programming languages created after 2010.",
    "What is the speed of light in km/s?",
)
_USERS = ("user-alice", "user-bob", "user-carol", "chat-user", "test-user")


def _post(base: str, model: str, prompt: str, user: str) -> dict:
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "context": {"user_id": user, "project_id": "proj-alpha"},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def _wait_until_ready(base: str, process: subprocess.Popen, log_path: Path) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            detail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"demo server exited during startup\n{detail}")
        try:
            with urllib.request.urlopen(f"{base}/api/models", timeout=1):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    raise RuntimeError(f"demo server did not become ready; see {log_path}")


def main() -> int:
    port = int(os.environ.get("AXON_SERVER_PORT", "8000"))
    base = f"http://localhost:{port}"
    environment = os.environ.copy()
    environment["AXON_LOAD_DEMO_DATA"] = "true"
    environment.setdefault("AXON_NO_BROWSER", "true")
    log_path = Path(tempfile.gettempdir()) / "axonllm-demo.log"

    print("AxonLLM demo")
    print(f"Starting the packaged gateway on {base}...")
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "src.gateway.local_server"],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        _wait_until_ready(base, process, log_path)
        success = 0
        failed = 0
        print("Generating provider traffic...")
        for index, model in enumerate(_MODELS):
            try:
                response = _post(
                    base,
                    model,
                    _PROMPTS[index % len(_PROMPTS)],
                    _USERS[index % len(_USERS)],
                )
                if "content" not in response:
                    raise RuntimeError(
                        response.get("error", {}).get("message", "no content")
                    )
                print(f"  {model:<28} OK ({response.get('provider', '?')})")
                success += 1
            except Exception as exc:
                print(f"  {model:<28} FAILED ({exc})")
                failed += 1

        print(f"\nDashboard:  {base}/admin/dashboard")
        print(f"Chat:       {base}/chat")
        print(f"Playground: {base}/playground")
        print(f"Providers tested: {success} successful, {failed} failed")
        if (
            os.environ.get("AXON_NO_BROWSER", "").lower()
            not in {"1", "true", "yes"}
            and sys.stdout.isatty()
        ):
            webbrowser.open(f"{base}/admin/dashboard")
        print("Press Ctrl+C to stop the server.")
        return process.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
