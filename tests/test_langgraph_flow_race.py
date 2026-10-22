"""A private read cannot cross an automatic publication's guarded flow decision."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from test_langgraph_adapter import authority as authority

from memory_worlds.errors import Forbidden
from memory_worlds.langgraph import MemoryWorldsAdapter
from memory_worlds.models import ScopeSelection


def test_automatic_publication_serializes_late_private_reads(authority):
    flow_checked, release_publication = Event(), Event()
    private_read_attempted, private_search_sent = Event(), Event()
    events = []
    shared = ScopeSelection(world_id=authority.world.id, scope_id=authority.world.scope_id)

    def control(request, response):
        if request.url.path == "/v1/scopes/flow" and not flow_checked.is_set():
            assert response.json()["allowed"] is True
            events.append("flow-checked")
            flow_checked.set()
            assert release_publication.wait(5), "Test must release the paused publication"
        if request.method == "POST" and request.url.path.endswith("/publications"):
            events.append("published")
        if request.url.path == "/v1/search":
            events.append("private-search")
            private_search_sent.set()
        return response

    # Two live adapter instances share an invocation journal as restarted or
    # parallel graph nodes would. Their credentials still go through the API.
    def runtime():
        return MemoryWorldsAdapter(
            authority.client_for(intercept=control), authority.directory
        ).execution(
            "parallel-execution",
            agent_id=authority.agent.id,
            read_scopes=[authority.selected],
            write_scope=shared,
        )

    publishing, reading = runtime(), runtime()
    original_record = reading._journal.record_exposure

    def observe_record(invocation_key, *, scopes=(), **kwargs):
        if any(str(item["scope_id"]) == str(authority.selected.scope_id) for item in scopes):
            private_read_attempted.set()
        return original_record(invocation_key, scopes=scopes, **kwargs)

    reading._journal.record_exposure = observe_record
    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(
            publishing.publish, "selected-before-read", "A public finding", project=False
        )
        try:
            assert flow_checked.wait(5), "Publication must pause after its original flow decision"
            search = pool.submit(reading.search, "private research", mode="source")
            assert private_read_attempted.wait(5), (
                "Concurrent read must attempt to record provenance"
            )
            assert not private_search_sent.wait(0.1), (
                "Private evidence cannot leave the API before publication finishes"
            )
        finally:
            release_publication.set()
        receipt = publication.result(timeout=5)
        search.result(timeout=5)

    assert receipt.episode.scope_id == shared.scope_id
    assert events.index("published") < events.index("private-search")
    assert any(
        item["scope_id"] == str(authority.selected.scope_id)
        for item in publishing.exposure()["read_scopes"]
    )
    # The first publication preceded the private read. Future publication must
    # honor the new exposure, including when the operation already has a receipt.
    with pytest.raises(Forbidden):
        publishing.retry("selected-before-read")
