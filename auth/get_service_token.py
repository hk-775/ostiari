"""Example: how a service/agent gets a token and calls Ostiari (client-credentials).

This runs on the CLIENT side (a service or agent), not inside Ostiari. It:
  1. reads the service's client_secret from AWS Secrets Manager (falls back to
     env for local/off-AWS testing),
  2. exchanges client_id + client_secret for a short-lived JWT at Cognito's
     token endpoint (OAuth2 client_credentials grant),
  3. calls an Ostiari gateway with `Authorization: Bearer <jwt>`.

Ostiari then validates that JWT locally via JWKS — it never sees the secret.

Config (env):
  COGNITO_TOKEN_URL   https://<domain>.auth.<region>.amazoncognito.com/oauth2/token
  COGNITO_CLIENT_ID   the M2M app client id
  COGNITO_SCOPE       e.g. ostiari/invoke
  SECRETS_MANAGER_ID  ARN/name of the secret holding the client_secret (preferred)
  COGNITO_CLIENT_SECRET   plain secret (fallback for local testing only)
  OSTIARI_GATEWAY     e.g. http://localhost:8421
  AGENT_ID            must match the token's agent identity + the X-Agent-Id header
"""

import os
import sys

import httpx


def read_client_secret() -> str:
    """Prefer Secrets Manager; fall back to an env var for local/off-AWS use."""
    secret_id = os.environ.get("SECRETS_MANAGER_ID")
    if secret_id:
        try:
            import boto3  # imported lazily so the script runs without boto3 locally
            sm = boto3.client("secretsmanager")
            resp = sm.get_secret_value(SecretId=secret_id)
            # Secret may be the raw string or JSON {"client_secret": "..."}.
            raw = resp.get("SecretString", "")
            if raw.strip().startswith("{"):
                import json
                return json.loads(raw)["client_secret"]
            return raw
        except Exception as e:  # noqa: BLE001
            print(f"  ! could not read Secrets Manager '{secret_id}': {e}", file=sys.stderr)
    env_secret = os.environ.get("COGNITO_CLIENT_SECRET")
    if env_secret:
        return env_secret
    print("  ! no client secret (set SECRETS_MANAGER_ID or COGNITO_CLIENT_SECRET)", file=sys.stderr)
    sys.exit(1)


def fetch_token() -> str:
    """OAuth2 client_credentials → short-lived access token (the service JWT)."""
    token_url = os.environ["COGNITO_TOKEN_URL"]
    client_id = os.environ["COGNITO_CLIENT_ID"]
    scope = os.environ.get("COGNITO_SCOPE", "ostiari/invoke")
    client_secret = read_client_secret()

    resp = httpx.post(
        token_url,
        data={"grant_type": "client_credentials", "scope": scope},
        auth=(client_id, client_secret),  # HTTP Basic — standard for this grant
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def call_ostiari(token: str) -> None:
    """Call a gateway tool with the bearer token (Ostiari validates it via JWKS)."""
    gateway = os.environ.get("OSTIARI_GATEWAY", "http://localhost:8421")
    agent_id = os.environ.get("AGENT_ID", "my-service")
    resp = httpx.post(
        f"{gateway}/tool/web_search",
        json={"query": "hello from a service token"},
        headers={
            "Authorization": f"Bearer {token}",
            "X-Agent-Id": agent_id,  # must match the token's asserted identity
            "Content-Type": "application/json",
        },
        timeout=10.0,
    )
    print(f"Ostiari responded {resp.status_code}: {resp.text[:200]}")


if __name__ == "__main__":
    tok = fetch_token()
    print(f"Got a token ({len(tok)} chars). Calling Ostiari…")
    call_ostiari(tok)
