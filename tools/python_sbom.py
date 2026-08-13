"""Generate a deterministic CycloneDX SBOM for the active Python environment."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path


def main() -> int:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "python-sbom.cdx.json")
    components = []
    for dist in sorted(
        metadata.distributions(),
        key=lambda item: (item.metadata["Name"] or "").lower(),
    ):
        name = dist.metadata["Name"]
        if not name:
            continue
        version = dist.version
        normalized = name.lower().replace("_", "-")
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": f"pkg:pypi/{normalized}@{version}",
            }
        )

    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:00000000-0000-4000-8000-000000000001",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "ostiari-python-sbom",
                        "version": "1",
                    }
                ]
            },
        },
        "components": components,
    }
    output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {len(components)} components to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
