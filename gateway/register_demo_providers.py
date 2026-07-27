"""Seed the control plane's LLM provider credentials from AxonLLM's .env.

The Providers page reads /api/providers, which is an in-memory store that nothing
seeds — so it renders empty on a fresh control plane even though /api/models is
auto-seeded with a dozen models. That mismatch shows models referencing providers
("anthropic", "openai", ...) that were never configured.

This reads the same env file the gateways already load (Makefile's LLM_ENV, default
../../AxonLLM/.env), so the UI ends up showing exactly the providers this machine
can actually reach — no invented credentials.

Idempotent: an existing provider is updated (PUT) rather than duplicated, so
re-running after a key rotation refreshes it.

Run after the control plane is up:
    python register_demo_providers.py
    python register_demo_providers.py --test    # also probe live connectivity

Key values are never printed — only whether a key was found, and the masked
preview the API itself returns.

NOTE: /api/providers is process-memory only (no DB table), so this must be re-run
after every control-plane restart. Keys are encrypted at rest with Fernet under
OSTIARI_ENCRYPTION_KEY; when that's unset the control plane mints a transient key,
which is fine for a demo but means the stored keys die with the process anyway.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONTROL_PLANE = os.environ.get("OSTIARI_CONTROL_PLANE", "http://localhost:8400")
LLM_ENV = Path(os.environ.get("LLM_ENV", "../../AxonLLM/.env"))
ADMIN_EMAIL = os.environ.get("OSTIARI_ADMIN_EMAIL", "admin@ostiari.ai")
ADMIN_PASSWORD = os.environ.get("OSTIARI_ADMIN_PASSWORD", "admin")

# env var -> provider name as the control plane and model registry know it.
# Only providers the control plane recognises are seeded; an unknown name would
# be stored but fail /test with "Unknown provider type".
PROVIDERS = [
    ("ANTHROPIC_API_KEY", "anthropic", {}),
    ("OPENAI_API_KEY", "openai", {}),
    ("XAI_API_KEY", "xai", {}),
    ("TOGETHER_API_KEY", "together", {}),
    # Google's Gemini models are registered under the "vertex" provider in the
    # model registry. Vertex proper wants project_id + region (service-account
    # auth); the AI Studio key here is a different credential, so it seeds
    # disabled rather than advertising a path that would fail at call time.
    ("GOOGLE_AI_API_KEY", "vertex", {"enabled": False}),
]

# Keys present in the env file with no provider slot in the control plane. Named
# explicitly so a reader can see they were considered, not overlooked.
UNSUPPORTED: dict[str, str] = {}


def _req(method: str, path: str, token: str = "", body: dict | list | None = None):
    """Call the control plane, returning (status, parsed_json_or_text)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{CONTROL_PLANE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw
    except urllib.error.URLError as e:
        print(f"ERROR: cannot reach the control plane at {CONTROL_PLANE}: {e.reason}")
        print("Start it first (make demo-full, or python main.py in control-plane/backend).")
        sys.exit(1)


def load_env(path: Path) -> dict[str, str]:
    """Parse KEY=value lines. Not a full shell parser — no expansion or `export`,
    matching what the Makefile's `set -a; . file` actually needs from these files."""
    if not path.is_file():
        print(f"ERROR: env file not found: {path.resolve()}")
        print("Set LLM_ENV=/path/to/.env to point somewhere else.")
        sys.exit(1)
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("'\"")
    return out


def login() -> str:
    """Admin token — POST/PUT /api/providers require the admin role."""
    status, body = _req("POST", "/api/auth/login",
                        body={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
        print(f"ERROR: admin login failed ({status}): {body}")
        print("Set OSTIARI_ADMIN_EMAIL / OSTIARI_ADMIN_PASSWORD if they differ from the demo defaults.")
        sys.exit(1)
    return body["access_token"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test", action="store_true",
                    help="probe each seeded provider's connectivity (makes a real API call)")
    args = ap.parse_args()

    env = load_env(LLM_ENV)
    print(f"Reading credentials from {LLM_ENV.resolve()}")
    token = login()

    existing = {p["name"] for p in (_req("GET", "/api/providers", token)[1] or [])}
    seeded, skipped = [], []

    for env_key, name, extra in PROVIDERS:
        key = env.get(env_key, "")
        if not key:
            skipped.append(f"{name} ({env_key} not set)")
            continue

        payload = {"name": name, "api_key": key, "enabled": True, **extra}
        if name in existing:
            # Idempotent refresh: PUT takes the same fields minus the name.
            status, body = _req("PUT", f"/api/providers/{name}", token,
                                {k: v for k, v in payload.items() if k != "name"})
            verb = "updated"
        else:
            status, body = _req("POST", "/api/providers", token, payload)
            verb = "added"

        if status == 200 and isinstance(body, dict):
            state = "enabled" if body.get("enabled") else "disabled"
            print(f"  {verb:8s} {name:10s} key={body.get('api_key_preview', '****')} ({state})")
            seeded.append(name)
        else:
            print(f"  FAILED   {name:10s} ({status}): {body}")

    for env_key, why in UNSUPPORTED.items():
        if env.get(env_key):
            skipped.append(why)

    if skipped:
        print("\nSkipped:")
        for s in skipped:
            print(f"  - {s}")

    if args.test and seeded:
        print("\nConnectivity:")
        for name in seeded:
            status, body = _req("POST", f"/api/providers/{name}/test", token)
            if status == 200 and isinstance(body, dict):
                ok = body.get("status") == "connected" or body.get("success")
                detail = body.get("error") or f"{body.get('latency_ms', '?')}ms"
                print(f"  {'OK  ' if ok else 'FAIL'} {name:10s} {detail}")
            else:
                print(f"  FAIL {name:10s} ({status}): {body}")

    print(f"\n{len(seeded)} provider(s) configured — visible on the Providers page.")
    print("Re-run after a control-plane restart: the provider store is in-memory only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
