"""Disposable kind identity fixture. Never use this development IdP in production."""

import json
import os
import sys
import tomllib
from pathlib import Path

from memory_worlds.models import CreateScope, EpisodeInput, ScopeKind
from memory_worlds.organization import OrganizationConfig
from memory_worlds.store import MemoryStore

directory = Path("/data/state")
receipt = Path("/data/qualification.json")
organization_path = Path("/data/organization.toml")


def organization_document(issuer):
    return f"""[identity]
issuer = {json.dumps(issuer)}
audience = "api://memory-worlds"
jwks_url = {json.dumps(issuer + "/protocol/openid-connect/certs")}
client_ids = ["mw-native", "mw-wrong-audience"]
client_id_claim = "azp"
required_scopes = ["memory-worlds"]
scopes_claim = "scope"
[spaces.company]
name = "Company"
kind = "company"
[spaces.data]
name = "Data"
kind = "team"
[roles.employee]
worlds = {{company = "reader"}}
[roles.data-member]
worlds = {{data = "writer"}}
[groups]
mw-company = ["employee"]
mw-data-team = ["data-member"]
"""


def bootstrap():
    store = MemoryStore(directory)
    world, token = store.bootstrap("synthetic-operator")
    operator = store.authenticate(token)
    organization = OrganizationConfig.model_validate(
        tomllib.loads(organization_document("https://qualification.invalid"))
    )
    organization.reconcile(store)
    worlds = {
        key: store.get_world(operator, organization.space_id(store.authority_id, key)).model_dump(
            mode="json"
        )
        for key in organization.spaces
    }
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

    issuer = os.environ["QUALIFICATION_ISSUER"]
    organization_path.write_text(organization_document(issuer))
    organization_path.chmod(0o600)
    os.environ.update(
        MEMORY_WORLDS_ORGANIZATION_FILE=str(organization_path),
        MEMORY_WORLDS_GRAPH_BACKEND="disabled",
    )
    uvicorn.run(create_app(directory), host="0.0.0.0", port=8787, log_level="warning")


if __name__ == "__main__":
    bootstrap() if sys.argv[1] == "bootstrap" else serve()
