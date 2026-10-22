"""Export public API metadata without loading local provider or authority settings."""

import json
import os
import tempfile
from pathlib import Path

from memory_worlds.api import create_app


def main():
    # This standalone process deliberately omits all private runtime settings.
    for key in list(os.environ):
        if key.startswith("MEMORY_WORLDS_"):
            del os.environ[key]
    with tempfile.TemporaryDirectory(prefix="memory-worlds-openapi-") as directory:
        app = create_app(Path(directory))
        schema = app.openapi()
        app.state.store.persistence.close()
    target = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
    target.write_text(json.dumps(schema, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {len(schema['paths'])} API paths to docs/openapi.json")


if __name__ == "__main__":
    main()
