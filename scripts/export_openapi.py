"""Export the API's OpenAPI schema with stable key order.

The web app generates its typed client from this file (see `make openapi`).
Usage: uv run python scripts/export_openapi.py [output-path]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sensei_api.app import create_app
from sensei_core.config import Settings

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "apps" / "web" / "src" / "lib" / "api" / "openapi.json"


def export(output: Path) -> Path:
    # The schema doesn't depend on the environment; test settings need no secrets (CI has none).
    schema = create_app(Settings(env="test")).openapi()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    print(f"wrote {export(target)}")
