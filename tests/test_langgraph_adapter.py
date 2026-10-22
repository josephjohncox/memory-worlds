"""Scoped workflow behavior against the actual HTTP authority and source journal."""

import json
import pickle
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from memory_worlds.api import create_app
from memory_worlds.client import Client
from memory_worlds.errors import Conflict, Forbidden
from memory_worlds.identity_names import CreateIdentity
from memory_worlds.langgraph import MemoryWorldsAdapter, WorkflowError
from memory_worlds.models import (
    CreateScope,
    CreateToken,
    DelegateCredential,
    ScopeGrant,
    ScopeKind,
    ScopeSelection,
    UpdateScopeGrant,
)
from memory_worlds.store import MemoryStore


@pytest.fixture
def authority(tmp_path):
    store = MemoryStore(tmp_path / "authority")
    world, admin_token = store.bootstrap("alice")
    owner = store.authenticate(admin_token)
    token = store.issue_token(
        owner, CreateToken(user_id="alice", agent_id="workflow", grants={world.id: "writer"})
    )
    clients, requests = [], []
    with TestClient(create_app(store.data_dir, store=store)) as api:

        def client_for(selected_token=token, intercept=None):
            client = Client("http://127.0.0.1:8787", selected_token)
            client.http.close()

            def transport(request):
                requests.append(request)
                response = api.request(
                    request.method,
                    str(request.url.copy_with(scheme="http", host="testserver")),
                    content=request.content,
                    headers=dict(request.headers),
                )
                result = httpx.Response(response.status_code, content=response.content)
                return intercept(request, result) if intercept else result

            client.http = httpx.Client(
                base_url="http://127.0.0.1:8787",
                headers={"Authorization": f"Bearer {selected_token}"},
                transport=httpx.MockTransport(transport),
            )
            clients.append(client)
            return client

        client = client_for()
        agent = client.create_identity("agent", CreateIdentity(name="Research specialist"))
        private = client.create_scope(
            world.id,
            CreateScope(kind=ScopeKind.AGENT_PERSISTENT, name="Research", agent_id=agent.id),
        )
        selected = ScopeSelection(world_id=world.id, scope_id=private.id)
        result = SimpleNamespace(
            store=store,
            world=world,
            owner=owner,
            admin_token=admin_token,
            token=token,
            client=client,
            client_for=client_for,
            requests=requests,
            agent=agent,
            selected=selected,
            directory=tmp_path / "workflow-journal",
            tmp_path=tmp_path,
        )
        try:
            yield result
        finally:
            for connection in clients:
                connection.http.close()


def execution(authority, *, client=None, directory=None, reads=None, key="execution-1"):
    adapter = MemoryWorldsAdapter(client or authority.client, directory or authority.directory)
    return adapter.execution(
        key,
        agent_id=authority.agent.id,
        read_scopes=[authority.selected] if reads is None else reads,
        write_scope=authority.selected,
    )


def _posts(authority, suffix):
    return [
        request
        for request in authority.requests
        if request.method == "POST" and request.url.path.endswith(suffix)
    ]


def delegated_worker(authority, parent, name, *, grants=()):
    agent = authority.client.create_identity("agent", CreateIdentity(name=name))
    delegated = authority.client.delegate(
        DelegateCredential(
            grants=list(grants),
            private_world_id=authority.world.id,
            agent_id=agent.id,
            run_id=parent.run_id,
            parent_context_id=parent.context.id,
        )
    )
    client = authority.client_for(delegated.token)
    adapter = MemoryWorldsAdapter(client, authority.tmp_path / name)
    worker = adapter.execution(
        name,
        agent_id=agent.id,
        run_id=parent.run_id,
        read_scopes=delegated.context.read_scopes,
        write_scope=delegated.context.write_scope,
        context_id=delegated.context.id,
    )
    return worker, delegated


def test_lifecycle_resumes_fixed_registered_agent_run_and_context(authority):
    first = execution(authority)
    first_context = first.context
    first_context.read_scopes.clear()
    assert first.context.read_scopes == [authority.selected]
    restarted = execution(authority, client=authority.client_for())
    assert restarted.context == first.context
    assert restarted.execution_id == first.execution_id
    assert restarted.run_id == first.run_id
    assert authority.client.identity("run", first.run_id).id == first.run_id
    assert len(_posts(authority, "/v1/contexts")) == 1
    assert any(
        request.method == "GET" and request.url.path == f"/v1/contexts/{first.context.id}"
        for request in authority.requests
    )
    with pytest.raises(TypeError, match="runtime context"):
        pickle.dumps(first._adapter)
    with pytest.raises(TypeError, match="runtime context"):
        pickle.dumps(first)


def test_uuid_names_do_not_register_agents_or_change_saved_scope_selection(authority):
    adapter = MemoryWorldsAdapter(authority.client, authority.directory)
    with pytest.raises(WorkflowError):
        adapter.execution(
            "unregistered", agent_id=uuid4(), read_scopes=[], write_scope=authority.selected
        )
    first = execution(authority)
    with pytest.raises(Conflict, match="changed parameters"):
        adapter.execution(
            "execution-1",
            agent_id=authority.agent.id,
            read_scopes=[],
            write_scope=authority.selected,
        )
    assert first.context.read_scopes == [authority.selected]


def test_operator_token_is_refused_before_workflow_journal_creation(authority):
    directory = authority.tmp_path / "operator-journal"
    with pytest.raises(Forbidden, match="ordinary"):
        MemoryWorldsAdapter(authority.client_for(authority.admin_token), directory)
    assert not directory.exists()


def test_lost_ack_replays_complete_original_intent_after_restart(authority):
    lost = False

    def lose_response(request, response):
        nonlocal lost
        if request.url.path.endswith("/episodes") and request.method == "POST" and not lost:
            lost = True
            assert response.status_code == 200
            raise httpx.ReadError("synthetic lost acknowledgement", request=request)
        return response

    worker = execution(authority, client=authority.client_for(intercept=lose_response))
    metadata = {"procedure": {"revision": 7}, "selected": ["verified finding"]}
    with pytest.raises(WorkflowError):
        worker.capture("finding-1", "A selected finding", metadata=metadata, project=False)
    assert (
        len(authority.client.episodes(authority.world.id, scope_id=authority.selected.scope_id))
        == 1
    )
    original_request = _posts(authority, "/episodes")[-1].content
    metadata["procedure"]["revision"] = 8
    restarted = execution(authority, client=authority.client_for())
    receipt = restarted.retry("finding-1")
    replay_request = _posts(authority, "/episodes")[-1].content
    assert json.loads(replay_request) == json.loads(original_request)
    assert receipt.replayed is True
    assert receipt.episode.context_id == worker.context.id
    assert receipt.episode.metadata["procedure"]["revision"] == 7
    assert receipt.episode.occurred_at.isoformat() == json.loads(original_request)[
        "occurred_at"
    ].replace("Z", "+00:00")
    assert (
        len(authority.client.episodes(authority.world.id, scope_id=authority.selected.scope_id))
        == 1
    )
    again = restarted.capture(
        "finding-1",
        "A selected finding",
        metadata={"procedure": {"revision": 7}, "selected": ["verified finding"]},
        project=False,
    )
    assert again.episode.id == receipt.episode.id


def test_large_unicode_source_remains_bounded_and_replays_after_restart(authority):
    content = "🧭" * 160_000
    worker = execution(authority)
    first = worker.capture("large-selected-finding", content, project=False)
    assert first.episode.content == content
    resumed = execution(authority, client=authority.client_for())
    retry = resumed.retry("large-selected-finding")
    assert retry.episode.id == first.episode.id
    assert retry.episode.content == content
    assert retry.episode.occurred_at == first.episode.occurred_at
    assert (
        resumed.capture("large-selected-finding", content, project=False).episode.id
        == first.episode.id
    )
    delivered = len(_posts(authority, "/episodes"))
    with pytest.raises(Conflict, match="changed parameters"):
        resumed.capture("large-selected-finding", "🔎" + content[1:], project=False)
    assert len(_posts(authority, "/episodes")) == delivered
    assert (
        len(authority.client.episodes(authority.world.id, scope_id=authority.selected.scope_id))
        == 1
    )
    assert all(path.stat().st_size <= 1_048_576 for path in authority.directory.glob("*.json"))


def test_mismatched_acknowledgement_is_not_saved_as_a_verified_receipt(authority):
    def corrupt_receipt(request, response):
        if request.method == "POST" and request.url.path.endswith("/episodes"):
            receipt = response.json()
            receipt["episode"]["metadata"] = {"unverified": "changed by transport"}
            return httpx.Response(200, json=receipt)
        return response

    worker = execution(authority, client=authority.client_for(intercept=corrupt_receipt))
    with pytest.raises(WorkflowError, match="original request fields"):
        worker.capture("selected", "Selected finding", metadata={"revision": 1}, project=False)
    restarted = execution(authority, client=authority.client_for())
    receipt = restarted.retry("selected")
    assert receipt.replayed is True
    assert receipt.episode.metadata == {"revision": 1}
    assert (
        len(authority.client.episodes(authority.world.id, scope_id=authority.selected.scope_id))
        == 1
    )


@pytest.mark.parametrize("change", ["content", "timestamp", "metadata", "project", "kind"])
def test_changed_write_retry_is_rejected_before_another_delivery(authority, change):
    worker = execution(authority)
    original = {
        "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
        "metadata": {"revision": 1},
        "project": False,
    }
    worker.capture("finding-1", "Original selected finding", **original)
    before = len(_posts(authority, "/episodes"))
    changed, content = dict(original), "Original selected finding"
    if change == "content":
        content = "Changed selected finding"
    elif change == "timestamp":
        changed["occurred_at"] = datetime(2026, 1, 2, tzinfo=UTC)
    elif change == "metadata":
        changed["metadata"] = {"revision": 2}
    elif change == "project":
        changed["project"] = True
    with pytest.raises(Conflict, match="changed parameters"):
        if change == "kind":
            worker.publish_selected(
                "finding-1",
                content,
                destination=authority.selected,
                approved=True,
                occurred_at=original["occurred_at"],
                project=False,
            )
        else:
            worker.capture("finding-1", content, **changed)
    assert len(_posts(authority, "/episodes")) == before


def test_cached_receipt_still_checks_live_credential_and_write_grant(authority):
    worker = execution(authority)
    receipt = worker.capture("finding-1", "Original selected finding", project=False)
    authority.client.grant_scope(
        authority.world.id,
        authority.selected.scope_id,
        UpdateScopeGrant(credential_id=worker.context.credential_id, operations=["read"]),
    )
    with pytest.raises(WorkflowError):
        worker.retry("finding-1")
    assert (
        authority.client.get_episode(
            authority.world.id, receipt.episode.id, scope_id=authority.selected.scope_id
        ).content
        == "Original selected finding"
    )


def test_revoked_credential_cannot_use_cached_receipt(authority):
    worker = execution(authority)
    worker.capture("finding-1", "Original selected finding", project=False)
    authority.store.revoke_token(authority.owner, worker.context.credential_id)
    with pytest.raises(WorkflowError):
        worker.retry("finding-1")


def test_missing_exposure_file_cannot_reset_a_resumed_workflow(authority):
    worker = execution(authority)
    worker.search("private research", mode="source")
    exposure_file = next(
        path
        for path in authority.directory.glob("*.json")
        if json.loads(path.read_text()).get("domain") == "exposure"
    )
    exposure_file.unlink()
    with pytest.raises(Conflict, match="exposure record is missing"):
        worker.capture("after-loss", "Selected finding", project=False)
    with pytest.raises(Conflict, match="exposure record is missing"):
        execution(authority, client=authority.client_for())
    assert not _posts(authority, "/episodes")


def test_rotating_to_another_valid_credential_cannot_reuse_the_runtime_binding(authority):
    worker = execution(authority)
    replacement = authority.store.issue_token(
        authority.owner,
        CreateToken(user_id="alice", agent_id="other-agent", grants={authority.world.id: "writer"}),
    )
    worker._client.http.headers["Authorization"] = "Bearer " + replacement
    with pytest.raises(Conflict, match="binding changed"):
        worker.capture("after-rotation", "Selected finding", project=False)
    assert not _posts(authority, "/episodes")
    assert authority.token not in "".join(
        path.read_text() for path in authority.directory.glob("*.json")
    )
    assert replacement not in "".join(
        path.read_text() for path in authority.directory.glob("*.json")
    )


def test_new_narrow_exposure_blocks_even_an_acknowledged_write(authority):
    peer_token = authority.store.issue_token(
        authority.owner,
        CreateToken(user_id="alice", agent_id="peer", grants={authority.world.id: "reader"}),
    )
    peer = authority.store.authenticate(peer_token)
    authority.client.grant_scope(
        authority.world.id,
        authority.selected.scope_id,
        UpdateScopeGrant(credential_id=peer.credential_id, operations=["read"]),
    )
    narrow = authority.client.create_scope(
        authority.world.id,
        CreateScope(kind=ScopeKind.INVOCATION_PRIVATE, name="Narrow notes"),
    )
    narrow_selection = ScopeSelection(world_id=authority.world.id, scope_id=narrow.id)
    worker = execution(authority, reads=[authority.selected, narrow_selection])
    worker.capture("finding-1", "Before private exposure", project=False)
    worker.search("nothing", mode="source")
    with pytest.raises(Forbidden, match="cannot flow"):
        worker.retry("finding-1")
    restarted = execution(
        authority, client=authority.client_for(), reads=[authority.selected, narrow_selection]
    )
    with pytest.raises(Forbidden, match="cannot flow"):
        restarted.retry("finding-1")


def test_failed_read_preserves_selected_scope_exposure_across_restart(authority):
    def unavailable(request, response):
        if request.url.path == "/v1/search":
            return httpx.Response(503, json={"detail": "synthetic failure"})
        return response

    worker = execution(authority, client=authority.client_for(intercept=unavailable))
    with pytest.raises(WorkflowError):
        worker.search("procedure", mode="source")
    restarted = execution(authority, client=authority.client_for())
    assert restarted.exposure() == {
        "authority_id": authority.world.authority_id,
        "read_scopes": [authority.selected.model_dump(mode="json")],
        "complete": True,
    }


@pytest.mark.parametrize("forgery", ["foreign-authority", "unselected-scope", "missing-provenance"])
def test_unverified_search_results_poison_automatic_writes(authority, forgery):
    def unverified(request, response):
        if request.url.path != "/v1/search":
            return response
        result = response.json()
        if forgery == "missing-provenance":
            result.pop("worlds")
        else:
            result["results"] = [
                {
                    "world_id": authority.world.id,
                    "scope_id": str(uuid4())
                    if forgery == "unselected-scope"
                    else str(authority.selected.scope_id),
                    "authority_id": "foreign"
                    if forgery == "foreign-authority"
                    else authority.world.authority_id,
                    "content": "unverified content must never escape",
                }
            ]
        return httpx.Response(200, json=result)

    worker = execution(authority, client=authority.client_for(intercept=unverified))
    with pytest.raises(WorkflowError, match="unverified provenance"):
        worker.search("procedure", mode="source")
    assert worker.exposure()["complete"] is False
    with pytest.raises(Forbidden, match="known exposure"):
        worker.capture("finding-1", "Cannot auto-capture unknown provenance", project=False)
    assert not _posts(authority, "/episodes")


def test_untrusted_worker_dictionary_permanently_marks_parent_exposure_unknown(authority):
    parent = execution(authority)
    with pytest.raises(WorkflowError, match="trusted live"):
        parent.absorb_worker("worker-1", {"complete": True, "read_scopes": []})
    assert parent.exposure()["complete"] is False
    with pytest.raises(Forbidden, match="known exposure"):
        parent.capture("after-forgery", "Selected finding", project=False)


def test_external_tool_exposure_stays_unknown_across_resume_and_cached_writes(authority):
    worker = execution(authority)
    worker.capture("before-external-tool", "Selected finding", project=False)
    exposure = worker.mark_unknown_exposure()
    assert exposure["complete"] is False
    with pytest.raises(Forbidden, match="known exposure"):
        worker.retry("before-external-tool")
    resumed = execution(authority, client=authority.client_for())
    assert resumed.exposure()["complete"] is False
    with pytest.raises(Forbidden, match="known exposure"):
        resumed.capture("after-external-tool", "Derived finding", project=False)
    assert len(_posts(authority, "/episodes")) == 1


def test_delegated_workers_attach_server_context_without_rebinding_or_run_registry_access(
    authority,
):
    parent = execution(authority)
    before = len(_posts(authority, "/v1/contexts"))
    worker, delegated = delegated_worker(authority, parent, "research-child")
    assert worker.context == delegated.context
    assert worker.context.parent_invocation_id == parent.context.invocation_id
    assert worker.context.agent_id != parent.context.agent_id
    assert worker.run_id == parent.run_id
    assert len(_posts(authority, "/v1/contexts")) == before
    with pytest.raises(httpx.HTTPStatusError) as unavailable:
        worker._client.identity("run", parent.run_id)
    assert unavailable.value.response.status_code == 404
    saved = worker.capture("working-note", "Private child working evidence", project=False)
    assert saved.episode.agent_uuid == delegated.context.agent_id
    assert saved.episode.invocation_id == delegated.context.invocation_id


def test_parent_and_sibling_have_no_implicit_private_scope_access(authority):
    parent = execution(authority)
    worker, delegated = delegated_worker(authority, parent, "research-child")
    sibling, _ = delegated_worker(authority, parent, "review-child")
    saved = worker.capture("working-note", "Private child working evidence", project=False)
    for reader in (parent._client, sibling._client):
        with pytest.raises(httpx.HTTPStatusError) as denied:
            reader.get_episode(
                authority.world.id, saved.episode.id, scope_id=delegated.private_scope.id
            )
        assert denied.value.response.status_code == 403
    with pytest.raises(WorkflowError, match="could not be verified"):
        sibling.absorb_worker("not-my-child", worker)
    assert sibling.exposure()["complete"] is False
    assert parent.exposure()["complete"] is True


def test_trusted_worker_findings_transfer_provenance_before_return(authority):
    parent = execution(authority)
    worker, delegated = delegated_worker(authority, parent, "research-child")

    def perform(memory):
        memory.capture("working-note", "Private child working evidence", project=False)
        return "Selected technical conclusion"

    result = parent.run_worker("research-1", worker, perform)
    assert result["result"] == "Selected technical conclusion"
    assert result["context"]["id"] == str(worker.context.id)
    private_selection = {
        "world_id": authority.world.id,
        "scope_id": str(delegated.private_scope.id),
    }
    assert private_selection in result["exposure"]["read_scopes"]
    assert result["exposure"]["complete"] is True
    restarted = execution(authority, client=authority.client_for())
    assert private_selection in restarted.exposure()["read_scopes"]
    with pytest.raises((Forbidden, WorkflowError)):
        restarted.capture("automatic-broadening", result["result"], project=False)


def test_worker_failure_still_transfers_its_exposure(authority):
    parent = execution(authority)
    worker, delegated = delegated_worker(authority, parent, "research-child")

    def fail(memory):
        memory.search("private research", mode="source")
        raise RuntimeError("synthetic worker failure")

    with pytest.raises(WorkflowError):
        parent.run_worker("failed-research", worker, fail)
    assert {
        "world_id": authority.world.id,
        "scope_id": str(delegated.private_scope.id),
    } in parent.exposure()["read_scopes"]


def test_explicit_publication_requires_approval_and_replays_frozen_context(authority):
    parent = execution(authority)
    destination = ScopeSelection(world_id=authority.world.id, scope_id=authority.world.scope_id)
    with pytest.raises(Forbidden, match="explicit approval"):
        parent.publish_selected(
            "selected", "Approved result", destination=destination, project=False
        )
    first = parent.publish_selected(
        "selected", "Approved result", destination=destination, approved=True, project=False
    )
    bindings = len(_posts(authority, "/v1/contexts"))
    second = execution(authority, client=authority.client_for()).retry("selected")
    assert second.episode.id == first.episode.id
    assert second.episode.context_id == first.episode.context_id
    assert len(_posts(authority, "/v1/contexts")) == bindings
    assert json.loads(_posts(authority, "/publications")[-1].content) == json.loads(
        _posts(authority, "/publications")[-2].content
    )


def test_publication_receipt_rechecks_publish_grant_after_revocation(authority):
    parent = execution(authority)
    target = authority.client.create_scope(
        authority.world.id, CreateScope(kind=ScopeKind.RUN_SHARED, name="Shared findings")
    )
    destination = ScopeSelection(world_id=authority.world.id, scope_id=target.id)
    child, delegated = delegated_worker(
        authority,
        parent,
        "publishing-child",
        grants=[ScopeGrant(**destination.model_dump(), operations=["read", "write", "publish"])],
    )
    receipt = child.publish_selected(
        "selected", "Approved result", destination=destination, approved=True, project=False
    )
    publication_context = child._client.get_context(receipt.episode.context_id)
    assert publication_context.run_id is None
    assert child.run_id == parent.run_id
    assert publication_context.agent_id == child.context.agent_id
    assert publication_context.invocation_id == child.context.invocation_id
    authority.client.grant_scope(
        destination.world_id,
        destination.scope_id,
        UpdateScopeGrant(
            credential_id=parent.context.credential_id,
            operations=["read", "write", "manage", "delegate"],
        ),
    )
    with pytest.raises(WorkflowError):
        child.retry("selected")
    assert (
        authority.client.get_episode(
            destination.world_id, receipt.episode.id, scope_id=destination.scope_id
        ).content
        == "Approved result"
    )


def test_automatic_publication_uses_shared_fixed_target_and_flow_guard(authority):
    shared = authority.client.create_scope(
        authority.world.id, CreateScope(kind=ScopeKind.RUN_SHARED, name="Shared findings")
    )
    destination = ScopeSelection(world_id=authority.world.id, scope_id=shared.id)
    adapter = MemoryWorldsAdapter(authority.client, authority.directory)
    worker = adapter.execution(
        "shared-target",
        agent_id=authority.agent.id,
        read_scopes=[destination],
        write_scope=destination,
    )
    receipt = worker.publish("selected", "Run-shared selected finding", project=False)
    assert receipt.episode.scope_id == destination.scope_id
    assert receipt.episode.context_id == worker.context.id
    assert _posts(authority, "/publications")
    assert not _posts(authority, "/episodes")


def test_automatic_shared_publication_refuses_private_exposure_without_selected_approval(authority):
    shared = authority.client.create_scope(
        authority.world.id, CreateScope(kind=ScopeKind.RUN_SHARED, name="Shared findings")
    )
    destination = ScopeSelection(world_id=authority.world.id, scope_id=shared.id)
    peer_token = authority.store.issue_token(
        authority.owner,
        CreateToken(user_id="alice", agent_id="peer", grants={authority.world.id: "reader"}),
    )
    peer = authority.store.authenticate(peer_token)
    authority.client.grant_scope(
        destination.world_id,
        destination.scope_id,
        UpdateScopeGrant(credential_id=peer.credential_id, operations=["read"]),
    )
    worker = MemoryWorldsAdapter(authority.client, authority.directory).execution(
        "shared-target",
        agent_id=authority.agent.id,
        read_scopes=[authority.selected],
        write_scope=destination,
    )
    worker.search("private research", mode="source")
    with pytest.raises(Forbidden, match="cannot flow"):
        worker.publish("automatic", "Private-derived findings", project=False)
    assert not _posts(authority, "/publications")
    selected = worker.publish_selected(
        "approved",
        "Explicitly approved selected finding",
        destination=destination,
        approved=True,
        project=False,
    )
    assert selected.episode.content == "Explicitly approved selected finding"


def test_concurrent_read_waits_until_automatic_write_acknowledgement(authority, monkeypatch):
    narrow = authority.client.create_scope(
        authority.world.id, CreateScope(kind=ScopeKind.INVOCATION_PRIVATE, name="Narrow notes")
    )
    reads = [authority.selected, ScopeSelection(world_id=authority.world.id, scope_id=narrow.id)]
    flow_started, permit_write = threading.Event(), threading.Event()
    read_attempted, read_finished, acknowledged = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    def pause_after_flow(request, response):
        if request.url.path == "/v1/scopes/flow":
            flow_started.set()
            assert permit_write.wait(10)
        if request.method == "POST" and request.url.path.endswith("/episodes"):
            acknowledged.set()
        return response

    worker = execution(
        authority, client=authority.client_for(intercept=pause_after_flow), reads=reads
    )
    record = worker._journal.record_exposure

    def observe_exposure(*args, **kwargs):
        if threading.current_thread().name.startswith("memory-reader"):
            read_attempted.set()
        return record(*args, **kwargs)

    monkeypatch.setattr(worker._journal, "record_exposure", observe_exposure)

    def read():
        try:
            return worker.search("procedure", mode="source")
        finally:
            read_finished.set()

    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-writer") as writers,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-reader") as readers,
    ):
        write = writers.submit(worker.capture, "before-read", "Selected finding", project=False)
        try:
            assert flow_started.wait(10)
            search = readers.submit(read)
            assert read_attempted.wait(10)
            assert not read_finished.wait(0.1)
            assert not acknowledged.is_set()
        finally:
            permit_write.set()
        assert write.result(timeout=10).episode.content == "Selected finding"
        assert search.result(timeout=10)["mode"] == "source"
        assert acknowledged.is_set()
    assert {"world_id": authority.world.id, "scope_id": str(narrow.id)} in worker.exposure()[
        "read_scopes"
    ]


def test_trace_sink_gets_ids_and_receipts_without_source_bodies(authority):
    records = []

    class Sink:
        def record(self, operation, correlation, **kwargs):
            records.append((operation, correlation, kwargs))

    adapter = MemoryWorldsAdapter(authority.client, authority.directory, trace_sink=Sink())
    worker = adapter.execution(
        "trace-workflow",
        agent_id=authority.agent.id,
        read_scopes=[authority.selected],
        write_scope=authority.selected,
    )
    receipt = worker.capture("selected", "PRIVATE TEST CONTENT", project=False)
    worker.search("PRIVATE TEST QUERY", mode="source")
    assert {record[0] for record in records} == {"capture", "search"}
    raw = json.dumps(records)
    assert "PRIVATE TEST CONTENT" not in raw
    assert "PRIVATE TEST QUERY" not in raw
    assert authority.token not in raw
    assert records[0][1]["episode_id"] == receipt.episode.id
    assert records[0][1]["invocation_id"] == str(worker.context.invocation_id)
