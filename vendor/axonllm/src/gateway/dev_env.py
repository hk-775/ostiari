"""Load provider API keys from a local ``.env`` file, for demos only.

``provider_loader`` reads API keys from ``os.environ``, and nothing populated it
from a file — so a ``.env`` sitting in the project root with five valid keys was
never consulted and every direct-API provider was silently skipped
(``load_provider_configs`` drops providers without credentials). The gateway
still answered, because Bedrock authenticates through AWS credentials on a
separate path, so the failure looked like "OpenAI is broken" rather than
"no keys were loaded".

This is deliberately *not* a general config mechanism. In production, secrets
arrive from the platform (ECS task definitions, Secrets Manager, App Runner
env), and a file that quietly shadowed those would be close to undebuggable:
the process would authenticate as something other than what the deploy
declared, with nothing in the logs to say so. Two properties keep that from
happening, and the second matters more than the first:

1. It only runs when the operator explicitly asked for demo mode.
2. **It never overwrites a variable that is already set.** The environment
   always wins. Even if this were invoked in production by mistake, injected
   credentials take precedence and the file is inert.

Hand-rolled rather than ``python-dotenv``: the whole parser is a few dozen lines
for a file format this narrow, and a demo-only convenience does not justify a
runtime dependency that ships in every production image.
"""

from __future__ import annotations

import logging
import os
from collections.abc import MutableMapping
from pathlib import Path

logger = logging.getLogger(__name__)

# os.environ is _Environ[str], not dict[str, str]. Accepting the wider mapping
# lets the same code path take the real environment and a test dict, and keeps
# the mutation below (env[name] = value) honest about writing through.
EnvMapping = MutableMapping[str, str]

# The file is only read when the operator explicitly set AXON_LOAD_DEMO_DATA
# themselves. The direct development entrypoint defaults that variable to
# "true" after this check, while container hosts select their profile explicitly.
_DEMO_FLAG = "AXON_LOAD_DEMO_DATA"
_PATH_OVERRIDE = "AXON_DEV_ENV_FILE"
_DEFAULT_PATH = ".env"


def demo_env_requested(environ: EnvMapping | None = None) -> bool:
    """True when the operator explicitly opted into demo mode.

    Checked against the environment *before* the dev entrypoint applies its own
    default, which is why this reads the raw value rather than ``AppConfig``.
    """
    env = os.environ if environ is None else environ
    return env.get(_DEMO_FLAG, "").strip().lower() == "true"


def parse_env_file(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines into a dict.

    Supports the subset of shell syntax a ``.env`` file actually uses: comments,
    blank lines, an optional ``export`` prefix, and single- or double-quoted
    values. Quoted values are taken verbatim, since an API key may legitimately
    contain a ``#``; unquoted values have trailing comments stripped.
    """
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()

        name, _, value = line.partition("=")
        name = name.strip()
        if not name.replace("_", "").isalnum():
            # Not an identifier — a stray line rather than an assignment.
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            # An unquoted trailing comment ends the value. Requiring whitespace
            # before the '#' keeps a '#' inside a key from truncating it.
            for sep in (" #", "\t#"):
                if sep in value:
                    value = value.split(sep, 1)[0].rstrip()
        result[name] = value
    return result


def load_dev_env_file(path: str | None = None, environ: EnvMapping | None = None) -> list[str]:
    """Populate ``environ`` from a ``.env`` file when demo mode was requested.

    Returns the names of the variables actually set — never their values, which
    are credentials and must stay out of logs. Returns an empty list when demo
    mode was not requested, the file is absent, or every name in it was already
    set in the environment.
    """
    env = os.environ if environ is None else environ

    if not demo_env_requested(env):
        return []

    target = path or env.get(_PATH_OVERRIDE) or _DEFAULT_PATH
    file_path = Path(target)
    if not file_path.is_file():
        # Absent is the normal case for a fresh clone; not worth a warning.
        logger.debug("No demo env file at %s", target)
        return []

    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read demo env file %s: %s", target, exc)
        return []

    loaded: list[str] = []
    skipped: list[str] = []
    for name, value in parse_env_file(text).items():
        if name in env:
            # The environment wins, always. This is the property that makes the
            # file safe: a real deploy's injected secrets are never shadowed.
            skipped.append(name)
            continue
        if not value:
            continue
        env[name] = value
        loaded.append(name)

    if loaded:
        logger.warning(
            "Demo mode: loaded %d variable(s) from %s — %s. "
            "This happens only because %s=true was set explicitly; "
            "production secrets should come from the platform environment.",
            len(loaded),
            target,
            ", ".join(sorted(loaded)),
            _DEMO_FLAG,
        )
    if skipped:
        logger.info(
            "Demo env file %s: kept existing environment value for %s",
            target,
            ", ".join(sorted(skipped)),
        )
    return loaded
