from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from memory_worlds.api import create_app
from memory_worlds.errors import Forbidden
from memory_worlds.errors import MemoryError as InvalidRequest
from memory_worlds.models import (
    MAX_IMPORT_BYTES,
    MAX_IMPORT_EPISODES,
    CreateScope,
    CreateToken,
    CreateWorld,
    DelegateCredential,
    EpisodeInput,
    ImportBatch,
    Role,
    ScopeGrant,
    ScopeKind,
    UpdateScopeGrant,
    WorldKind,
)
from memory_worlds.store import MemoryStore


def episode(source="message-1", **changes):
    return EpisodeInput(
        source_id=source,
        content="A bounded retry interval is five seconds.",
        occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
        **changes,
    )


def payload(*episodes):
    return {"episodes": [item.model_dump(mode="json") for item in episodes]}


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def local(tmp_path):
    store = MemoryStore(tmp_path)
    world, token = store.bootstrap("alice")
    owner = store.authenticate(token)
    scope = store.create_scope(
        owner,
        world.id,
        CreateScope(kind=ScopeKind.AGENT_PERSISTENT, name="Private transcript archive"),
    )
    return store, world, owner, scope, token


def path(world, scope):
    return f"/v1/worlds/{world.id}/scopes/{scope.id}/imports"


def credential(store, owner, world, scope, *, role=Role.WRITER, operations=("read", "write")):
    token = store.issue_token(
        owner, CreateToken(user_id="alice", agent_id="importer", grants={world.id: role})
    )
    principal = store.authenticate(token)
    store.set_scope_grant(
        owner,
        world.id,
        scope.id,
        UpdateScopeGrant(credential_id=principal.credential_id, operations=list(operations)),
    )
    return principal, token


def journal_counts(store, world, scope):
    with store._world_db(world.id, scope.id) as db:
        return tuple(
            db.execute(select(func.count()).select_from(store._table(db, table))).scalar_one()
            for table in ("episodes", "audit", "projection_jobs")
        )


def test_writer_import_is_private_atomic_durable_and_queues_graphiti_by_default(local, tmp_path):
    store, world, owner, scope, _ = local
    writer, token = credential(store, owner, world, scope)
    notes = [
        episode("claude-message", source_type="claude", role="assistant"),
        episode("source-only-message", source_type="chatgpt", project=False),
    ]
    with TestClient(create_app(tmp_path)) as api:
        response = api.post(path(world, scope), json=payload(*notes), headers=auth(token))
        assert response.status_code == 200, response.text
        assert response.json() == {"imported": 2, "replayed": 0}
        assert api.post(path(world, scope), json=payload(*notes), headers=auth(token)).json() == {
            "imported": 0,
            "replayed": 2,
        }
    reopened = MemoryStore(tmp_path)
    rows = reopened.list_episodes(writer, world.id, scope_id=scope.id)
    assert [row.source_type for row in rows] == ["claude", "chatgpt"]
    assert [row.projection_state for row in rows] == ["pending", "disabled"]
    assert all(row.scope_id == scope.id and row.context_id is None for row in rows)
    assert all(row.actor_id == "alice" and row.agent_id == "importer" for row in rows)
    assert not reopened.list_episodes(owner, world.id)
    assert journal_counts(reopened, world, scope) == (2, 2, 1)


@pytest.mark.parametrize("kind", [ScopeKind.WORLD_SHARED, ScopeKind.RUN_SHARED])
def test_import_rejects_shared_scope_even_in_owned_personal_world(local, tmp_path, kind):
    store, world, owner, _, token = local
    scope = store.create_scope(owner, world.id, CreateScope(kind=kind, name="Shared"))
    with TestClient(create_app(tmp_path)) as api:
        response = api.post(path(world, scope), json=payload(episode()), headers=auth(token))
    assert response.status_code == 403
    assert journal_counts(store, world, scope) == (0, 0, 0)


@pytest.mark.parametrize("kind", [WorldKind.TEAM, WorldKind.COMPANY, WorldKind.AD_HOC])
def test_import_rejects_shared_world_even_with_private_scope_and_admin(local, tmp_path, kind):
    store, _, owner, _, token = local
    world = store.create_world(owner, CreateWorld(name="Shared organization", kind=kind))
    scope = store.create_scope(
        owner, world.id, CreateScope(kind=ScopeKind.INVOCATION_PRIVATE, name="Private scope")
    )
    with TestClient(create_app(tmp_path)) as api:
        response = api.post(path(world, scope), json=payload(episode()), headers=auth(token))
    assert response.status_code == 403
    assert journal_counts(store, world, scope) == (0, 0, 0)


def test_import_requires_world_ownership_even_for_an_administrator(local, tmp_path):
    store, _, owner, _, token = local
    world = store.create_world(owner, CreateWorld(name="Bob private", owner_id="bob"))
    scope = store.create_scope(
        owner, world.id, CreateScope(kind=ScopeKind.AGENT_PERSISTENT, name="Explicit grant")
    )
    with TestClient(create_app(tmp_path)) as api:
        response = api.post(path(world, scope), json=payload(episode()), headers=auth(token))
    assert response.status_code == 403
    assert journal_counts(store, world, scope) == (0, 0, 0)


@pytest.mark.parametrize(
    ("role", "operations"), [(Role.READER, ("read", "write")), (Role.WRITER, ("read",))]
)
def test_world_and_scope_write_grants_are_both_required(local, tmp_path, role, operations):
    store, world, owner, scope, _ = local
    _, token = credential(store, owner, world, scope, role=role, operations=operations)
    with TestClient(create_app(tmp_path)) as api:
        response = api.post(path(world, scope), json=payload(episode()), headers=auth(token))
    assert response.status_code == 403
    assert journal_counts(store, world, scope) == (0, 0, 0)


def test_delegated_import_cannot_reach_parent_or_sibling_private_memory(local, tmp_path):
    store, world, owner, owner_scope, owner_token = local
    child = store.delegate_credential(owner, DelegateCredential(private_world_id=world.id))
    sibling = store.delegate_credential(owner, DelegateCredential(private_world_id=world.id))
    child_principal = store.authenticate(child["token"])
    own_scope = child["private_scope"]
    with TestClient(create_app(tmp_path)) as api:
        response = api.post(
            path(world, own_scope), json=payload(episode()), headers=auth(child["token"])
        )
        assert response.status_code == 200, response.text
        for target in (owner_scope, sibling["private_scope"]):
            assert (
                api.post(
                    path(world, target), json=payload(episode()), headers=auth(child["token"])
                ).status_code
                == 403
            )
        assert (
            api.post(
                path(world, own_scope), json=payload(episode("parent")), headers=auth(owner_token)
            ).status_code
            == 403
        )
        store.revoke_token(owner, child_principal.credential_id)
        assert (
            api.post(
                path(world, own_scope),
                json=payload(episode("revoked")),
                headers=auth(child["token"]),
            ).status_code
            == 401
        )
    row = store.pending_jobs(world.id, own_scope.id)[0]
    assert row.agent_uuid == child_principal.agent_uuid
    assert row.invocation_id == child_principal.invocation_id
    assert journal_counts(store, world, own_scope) == (1, 1, 1)


def test_import_rechecks_attenuated_parent_scope_grants(local, tmp_path):
    store, world, owner, scope, _ = local
    issued = store.delegate_credential(
        owner,
        DelegateCredential(
            grants=[ScopeGrant(world_id=world.id, scope_id=scope.id, operations=["read", "write"])]
        ),
    )
    with TestClient(create_app(tmp_path)) as api:
        assert (
            api.post(
                path(world, scope), json=payload(episode()), headers=auth(issued["token"])
            ).status_code
            == 200
        )
        store.set_scope_grant(
            owner,
            world.id,
            scope.id,
            UpdateScopeGrant(
                credential_id=owner.credential_id, operations=["read", "delegate", "manage"]
            ),
        )
        assert (
            api.post(
                path(world, scope),
                json=payload(episode("after-grant-change")),
                headers=auth(issued["token"]),
            ).status_code
            == 403
        )
    assert journal_counts(store, world, scope) == (1, 1, 1)


def test_live_cross_user_reader_blocks_import_but_same_user_agents_and_revoked_readers_do_not(
    local, tmp_path
):
    store, world, owner, scope, token = local
    credential(store, owner, world, scope, role=Role.READER, operations=("read",))
    foreign_token = store.issue_token(
        owner, CreateToken(user_id="bob", agent_id="administrator", admin=True)
    )
    foreign = store.authenticate(foreign_token)
    store.set_scope_grant(
        owner,
        world.id,
        scope.id,
        UpdateScopeGrant(credential_id=foreign.credential_id, operations=["read"]),
    )
    with TestClient(create_app(tmp_path)) as api:
        assert (
            api.post(path(world, scope), json=payload(episode()), headers=auth(token)).status_code
            == 403
        )
        assert journal_counts(store, world, scope) == (0, 0, 0)
        store.revoke_token(owner, foreign.credential_id)
        response = api.post(path(world, scope), json=payload(episode()), headers=auth(token))
        assert response.status_code == 200, response.text
    assert journal_counts(store, world, scope) == (1, 1, 1)


@pytest.mark.parametrize("damage", ["scope", "context", "publication", "invalid-final"])
def test_invalid_later_record_never_imports_an_earlier_record(local, tmp_path, damage):
    store, world, _, scope, token = local
    body = payload(episode("first"), episode("last"))
    if damage == "scope":
        body["episodes"][-1]["scope_id"] = str(uuid4())
    elif damage == "context":
        body["episodes"][-1]["context_id"] = str(uuid4())
    elif damage == "publication":
        body["episodes"][-1]["source_type"] = "publication"
    else:
        body["episodes"][-1]["occurred_at"] = "private-marker-invalid-date"
    with TestClient(create_app(tmp_path)) as api:
        response = api.post(path(world, scope), json=body, headers=auth(token))
    assert response.status_code == (422 if damage == "invalid-final" else 403)
    assert "private-marker" not in response.text
    assert journal_counts(store, world, scope) == (0, 0, 0)


def test_explicit_matching_episode_scope_is_normalized_to_the_same_replay_identity(local):
    store, world, owner, scope, _ = local
    assert store.import_episodes(owner, world.id, scope.id, [episode()]) == {
        "imported": 1,
        "replayed": 0,
    }
    assert store.import_episodes(owner, world.id, scope.id, [episode(scope_id=scope.id)]) == {
        "imported": 0,
        "replayed": 1,
    }


def test_conflicting_later_source_rolls_back_prior_episodes_audit_and_graph_jobs(local, tmp_path):
    store, world, owner, scope, token = local
    store.import_episodes(owner, world.id, scope.id, [episode("existing")])
    changed = episode("existing").model_copy(update={"content": "Conflicting record"})
    with TestClient(create_app(tmp_path)) as api:
        response = api.post(
            path(world, scope), json=payload(episode("first"), changed), headers=auth(token)
        )
    assert response.status_code == 409
    assert [row.source_id for row in store.list_episodes(owner, world.id, scope_id=scope.id)] == [
        "existing"
    ]
    assert journal_counts(store, world, scope) == (1, 1, 1)


def test_concurrent_identical_imports_commit_once_and_replay_without_duplicate_jobs(
    local, tmp_path
):
    store, world, _, scope, token = local
    with TestClient(create_app(tmp_path)) as api:

        def send(_):
            return api.post(
                path(world, scope),
                json=payload(episode("first"), episode("second")),
                headers=auth(token),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(send, range(2)))
    assert all(response.status_code == 200 for response in responses)
    assert sorted(response.json()["imported"] for response in responses) == [0, 2]
    assert sorted(response.json()["replayed"] for response in responses) == [0, 2]
    assert journal_counts(store, world, scope) == (2, 2, 2)


def test_import_schema_and_store_bound_record_count_before_writes(local, tmp_path):
    store, world, owner, scope, token = local
    assert len(ImportBatch(episodes=[episode()] * MAX_IMPORT_EPISODES).episodes) == 10_000
    for count in (0, MAX_IMPORT_EPISODES + 1):
        with pytest.raises(ValidationError):
            ImportBatch(episodes=[episode()] * count)
        with pytest.raises(InvalidRequest):
            store.import_episodes(owner, world.id, scope.id, [episode()] * count)
        with TestClient(create_app(tmp_path)) as api:
            response = api.post(
                path(world, scope), json=payload(*([episode()] * count)), headers=auth(token)
            )
            assert response.status_code == 422
    assert journal_counts(store, world, scope) == (0, 0, 0)


def test_32_mib_body_limit_applies_only_to_the_exact_post_import_route(local, tmp_path):
    store, world, _, scope, token = local
    with TestClient(create_app(tmp_path)) as api:
        # A legitimate import larger than the ordinary 1 MiB limit succeeds.
        notes = [
            episode(f"large-{index}").model_copy(update={"content": "x" * 200_000})
            for index in range(6)
        ]
        response = api.post(path(world, scope), json=payload(*notes), headers=auth(token))
        assert response.status_code == 200, response.text
        assert response.json()["imported"] == 6
        assert (
            api.post(
                path(world, scope), content=b"x" * (MAX_IMPORT_BYTES + 1), headers=auth(token)
            ).status_code
            == 413
        )
        for method, target in (
            ("POST", "/v1/search"),
            ("GET", path(world, scope)),
            ("POST", path(world, scope) + "/other"),
            ("POST", f"/v1/worlds/{world.id}/scopes/not-a-uuid/imports"),
        ):
            assert (
                api.request(
                    method, target, content=b"x" * 1_048_577, headers=auth(token)
                ).status_code
                == 413
            )
    assert journal_counts(store, world, scope) == (6, 6, 6)


def test_store_rejects_oversize_normalized_payload_and_nonfinite_metadata_before_writes(local):
    store, world, owner, scope, _ = local
    for note in (
        episode(metadata={"oversize": "x" * MAX_IMPORT_BYTES}),
        episode(metadata={"bad": float("nan")}),
        episode(metadata={"bad": "\ud800"}),
    ):
        with pytest.raises(InvalidRequest):
            store.import_episodes(owner, world.id, scope.id, [episode("first"), note])
        assert journal_counts(store, world, scope) == (0, 0, 0)


def test_direct_import_rejects_an_explicit_foreign_scope_without_opening_it(local):
    store, world, owner, scope, _ = local
    with pytest.raises(Forbidden):
        store.import_episodes(owner, world.id, scope.id, [episode(scope_id=uuid4())])
    assert journal_counts(store, world, scope) == (0, 0, 0)
