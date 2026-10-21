"""Disposable kind identity fixture. Never use this development IdP in production."""

import json
import os
import sys
from pathlib import Path

from memory_worlds.models import CreateScope, CreateWorld, EpisodeInput, ScopeKind, WorldKind
from memory_worlds.store import MemoryStore

directory = Path("/data/state")
receipt = Path("/data/qualification.json")


def bootstrap():
    store = MemoryStore(directory)
    world, token = store.bootstrap("synthetic-operator")
    operator = store.authenticate(token)
    worlds = {}
    for name, kind in (("Company", WorldKind.COMPANY), ("Data", WorldKind.TEAM)):
        created = store.create_world(operator, CreateWorld(name=name, kind=kind))
        worlds[name.lower()] = created.model_dump(mode="json")
    private = store.create_scope(
        operator,
        world.id,
        CreateScope(kind=ScopeKind.INVOCATION_PRIVATE, name="Synthetic operator scratch"),
    )
    store.capture(
        operator,
        world.id,
        EpisodeInput(
            scope_id=private.id,
            source_id="qualification-private-fixture",
            content="Synthetic operator note stays outside OIDC shared groups.",
            occurred_at="2026-10-20T12:00:00Z",
            project=False,
        ),
    )
    receipt.write_text(
        json.dumps(
            {
                "operator_token": token,
                "operator_world": world.id,
                "operator_scope": str(private.id),
                "worlds": worlds,
            }
        )
    )
    receipt.chmod(0o600)


def serve():
    import uvicorn

    from memory_worlds.api import create_app

    data = json.loads(receipt.read_text())
    issuer = os.environ["QUALIFICATION_ISSUER"]
    os.environ.update(
        MEMORY_WORLDS_OIDC_ISSUER=issuer,
        MEMORY_WORLDS_OIDC_JWKS_URL=f"{issuer}/protocol/openid-connect/certs",
        MEMORY_WORLDS_OIDC_AUDIENCE="api://memory-worlds",
        MEMORY_WORLDS_OIDC_CLIENT_IDS="mw-native,mw-wrong-audience",
        MEMORY_WORLDS_OIDC_CLIENT_ID_CLAIM="azp",
        MEMORY_WORLDS_OIDC_REQUIRED_SCOPES="memory-worlds",
        MEMORY_WORLDS_OIDC_SCOPES_CLAIM="scope",
        MEMORY_WORLDS_OIDC_GROUP_MAPPINGS=json.dumps(
            {
                "mw-company": {data["worlds"]["company"]["id"]: "reader"},
                "mw-data-team": {data["worlds"]["data"]["id"]: "writer"},
            }
        ),
        MEMORY_WORLDS_GRAPH_BACKEND="disabled",
    )
    uvicorn.run(create_app(directory), host="0.0.0.0", port=8787, log_level="warning")


if __name__ == "__main__":
    bootstrap() if sys.argv[1] == "bootstrap" else serve()
