"""Durable adapter state survives process loss without weakening scope provenance."""

import json
import multiprocessing
import os
import stat
import time
from pathlib import Path
from uuid import uuid4

import pytest

from memory_worlds.errors import Conflict, MemoryError
from memory_worlds.langgraph_journal import Journal

_BINDING = {
    "service_url": "http://localhost:8787",
    "authority_id": "authority_a",
    "credential_id": "credential_a",
}


def _journal(path: Path) -> Journal:
    return Journal(path, **_BINDING)


def _qualified(*, authority="authority_a", world="personal", scope=None):
    return {"authority_id": authority, "world_id": world, "scope_id": scope or str(uuid4())}


def _freeze_worker(directory, start, results):
    journal = _journal(Path(directory))
    start.wait(10)

    def create():
        with open(Path(directory) / "factory-calls", "a") as output:
            output.write("create\n")
        time.sleep(0.05)
        return {"context_id": str(uuid4())}

    results.put(journal.freeze("worker", "research", {"run": "execution-1"}, create))


def _exposure_worker(directory, scope, start, results):
    journal = _journal(Path(directory))
    start.wait(10)
    journal.record_exposure("invocation-1", scopes=[scope])
    results.put(True)


def _guarded_exposure_worker(directory, invocation, scope, started, finished):
    journal = _journal(Path(directory))
    started.set()
    journal.record_exposure(invocation, scopes=[scope])
    finished.set()


def test_freeze_survives_restart_and_rejects_changed_semantic_input(tmp_path):
    directory = tmp_path / "journal"
    original = {
        "context_id": str(uuid4()),
        "occurred_at": "2026-10-21T00:00:00Z",
        "metadata": {"worker": "research"},
        "content": "Selected finding",
    }
    frozen = _journal(directory).freeze(
        "capture", "decision-1", {"content": "Selected finding"}, lambda: original
    )
    frozen["metadata"]["worker"] = "mutated return value"
    original["content"] = "mutated caller object"
    restarted = _journal(directory)
    recovered = restarted.freeze(
        "capture",
        "decision-1",
        {"content": "Selected finding"},
        lambda: pytest.fail("a retry must use the complete original payload"),
    )
    assert recovered["content"] == "Selected finding"
    assert recovered["metadata"] == {"worker": "research"}
    assert restarted.get("capture", "decision-1") == recovered
    with pytest.raises(Conflict, match="changed parameters"):
        restarted.freeze("capture", "decision-1", {"content": "different"}, lambda: {})


def test_record_identity_domains_and_receipts_are_separate(tmp_path):
    journal = _journal(tmp_path / "journal")
    payload = journal.freeze("capture", "same-key", {"input": "a"}, lambda: {"body": "a"})
    receipt = journal.freeze(
        "receipt", "same-key", {"intent_digest": "digest-a"}, lambda: {"episode_id": "source-1"}
    )
    assert journal.get("capture", "same-key") == payload
    assert journal.get("receipt", "same-key") == receipt
    with pytest.raises(Conflict, match="changed parameters"):
        journal.freeze("receipt", "same-key", {"intent_digest": "digest-b"}, lambda: {})
    assert journal.get("capture", "never-created") is None


@pytest.mark.parametrize("field", ["service_url", "authority_id", "credential_id"])
def test_journal_cannot_be_rebound_to_another_identity(tmp_path, field):
    directory = tmp_path / "journal"
    journal = _journal(directory)
    journal.freeze("execution", "workflow-1", {}, lambda: {"id": "original"})
    changed = dict(_BINDING)
    changed[field] = "http://localhost:8788" if field == "service_url" else "another_identity"
    with pytest.raises(Conflict, match="another service or credential"):
        Journal(directory, **changed)
    assert _journal(directory).get("execution", "workflow-1") == {"id": "original"}


def test_binding_tampering_fails_closed_for_an_existing_runtime(tmp_path):
    directory = tmp_path / "journal"
    journal = _journal(directory)
    changed = dict(_BINDING, authority_id="other")
    (directory / "binding.json").write_text(json.dumps(changed))
    with pytest.raises(Conflict, match="binding changed"):
        journal.freeze("capture", "new", {}, lambda: pytest.fail("must not run"))


def test_failed_factory_does_not_publish_a_partial_record(tmp_path):
    journal = _journal(tmp_path / "journal")

    def fail():
        raise RuntimeError("remote response was lost")

    with pytest.raises(RuntimeError, match="lost"):
        journal.freeze("worker", "new", {}, fail)
    assert journal.get("worker", "new") is None
    assert journal.freeze("worker", "new", {}, lambda: {"id": "saved"}) == {"id": "saved"}


def test_separate_processes_create_one_immutable_lifecycle_record(tmp_path):
    directory = tmp_path / "journal"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(target=_freeze_worker, args=(directory, start, results)) for _ in range(3)
    ]
    try:
        for worker in workers:
            worker.start()
        start.set()
        values = [results.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(15)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
        results.close()
    assert values[0] == values[1] == values[2]
    assert (directory / "factory-calls").read_text() == "create\n"


def test_exposure_is_monotonic_and_unknown_cannot_be_cleared(tmp_path):
    directory = tmp_path / "journal"
    first, second = _qualified(), _qualified(authority="authority_b", world="team")
    journal = _journal(directory)
    assert journal.exposure("invocation-1") == {"scopes": [], "authorities": [], "unknown": False}
    journal.record_exposure("invocation-1", scopes=[first], unknown=True)
    restarted = _journal(directory)
    value = restarted.record_exposure(
        "invocation-1", scopes=[second, first], authorities=["authority_c"], unknown=False
    )
    assert value["scopes"] == [first, second]
    assert value["authorities"] == ["authority_a", "authority_b", "authority_c"]
    assert value["unknown"] is True
    assert restarted.exposure("invocation-1") == value
    assert restarted.exposure("another-invocation")["scopes"] == []
    with pytest.raises(MemoryError, match="monotonic"):
        restarted.freeze("exposure", "invocation-1", {}, lambda: {})


def test_parallel_worker_handoffs_do_not_lose_provenance(tmp_path):
    directory = tmp_path / "journal"
    scopes = [_qualified() for _ in range(3)]
    context = multiprocessing.get_context("spawn")
    start, results = context.Event(), context.Queue()
    workers = [
        context.Process(target=_exposure_worker, args=(directory, scope, start, results))
        for scope in scopes
    ]
    try:
        for worker in workers:
            worker.start()
        start.set()
        assert all(results.get(timeout=15) for _ in workers)
        for worker in workers:
            worker.join(15)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
        results.close()
    assert _journal(directory).exposure("invocation-1")["scopes"] == sorted(
        scopes, key=lambda scope: scope["scope_id"]
    )


def test_invocation_guard_blocks_late_exposure_through_acknowledged_write(tmp_path):
    directory = tmp_path / "journal"
    journal = _journal(directory)
    first, later = _qualified(), _qualified()
    context = multiprocessing.get_context("spawn")
    started, finished = context.Event(), context.Event()
    worker = context.Process(
        target=_guarded_exposure_worker,
        args=(directory, "invocation-1", later, started, finished),
    )
    try:
        with journal.invocation_guard("invocation-1"):
            # Reentrancy spans instances in this thread. Otherwise the adapter's
            # record-before-POST operation would deadlock on its own guard.
            _journal(directory).record_exposure("invocation-1", scopes=[first])
            worker.start()
            assert started.wait(10)
            assert not finished.wait(0.1)
            assert journal.exposure("invocation-1")["scopes"] == [first]
            # The HTTP write acknowledgement would arrive before this guard
            # exits; the other process cannot change its exposure decision.
        assert finished.wait(10)
        worker.join(10)
        assert worker.exitcode == 0
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
    assert _journal(directory).exposure("invocation-1")["scopes"] == sorted(
        [first, later], key=lambda scope: scope["scope_id"]
    )


def test_invocation_guard_is_independent_and_released_after_failure(tmp_path):
    directory = tmp_path / "journal"
    journal = _journal(directory)
    context = multiprocessing.get_context("spawn")
    started, finished = context.Event(), context.Event()
    worker = context.Process(
        target=_guarded_exposure_worker,
        args=(directory, "other-invocation", _qualified(), started, finished),
    )
    try:
        with pytest.raises(RuntimeError, match="simulated"):
            with journal.invocation_guard("invocation-1"):
                worker.start()
                assert started.wait(10)
                assert finished.wait(10)
                raise RuntimeError("simulated HTTP failure")
        worker.join(10)
        assert worker.exitcode == 0
        with _journal(directory).invocation_guard("invocation-1"):
            journal.record_exposure("invocation-1", unknown=True)
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
    assert journal.exposure("invocation-1")["unknown"] is True


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), object(), {1: "key"}, ("tuple",)])
def test_non_json_capture_values_are_never_frozen(tmp_path, bad):
    journal = _journal(tmp_path / "journal")
    with pytest.raises(MemoryError, match="finite JSON"):
        journal.freeze("capture", "source", {}, lambda: {"bad": bad})
    assert journal.get("capture", "source") is None


def test_bounded_private_files_and_no_credential_secret_field(tmp_path):
    directory = tmp_path / "journal"
    journal = _journal(directory)
    journal.freeze("capture", "private-source", {}, lambda: {"content": "private"})
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for path in directory.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads((directory / "binding.json").read_bytes()) == _BINDING
    with pytest.raises(MemoryError, match="size limit"):
        journal.freeze("capture", "oversized", {}, lambda: {"content": "x" * 1_048_576})
    assert journal.get("capture", "oversized") is None


@pytest.mark.parametrize("replacement", ["symlink", "hardlink", "fifo", "public"])
def test_unsafe_record_is_rejected_without_altering_the_original(tmp_path, replacement):
    directory = tmp_path / "journal"
    journal = _journal(directory)
    journal.freeze("capture", "source", {}, lambda: {"content": "original"})
    record = next(path for path in directory.glob("*.json") if path.name != "binding.json")
    before = record.read_bytes()
    original = tmp_path / "original"
    original.write_bytes(before)
    original.chmod(0o600)
    record.unlink()
    if replacement == "symlink":
        record.symlink_to(original)
    elif replacement == "hardlink":
        os.link(original, record)
    elif replacement == "fifo":
        os.mkfifo(record, 0o600)
    else:
        record.write_bytes(before)
        record.chmod(0o644)
    with pytest.raises(MemoryError):
        journal.get("capture", "source")
    assert original.read_bytes() == before


def test_unqualified_or_forged_ledger_fields_are_rejected(tmp_path):
    journal = _journal(tmp_path / "journal")
    with pytest.raises(MemoryError, match="qualified"):
        journal.record_exposure("invocation", scopes=[{"scope_id": str(uuid4())}])
    with pytest.raises(MemoryError, match="invalid"):
        journal.record_exposure("invocation", unknown="false")
    with pytest.raises(MemoryError, match="UUID"):
        journal.record_exposure("invocation", scopes=[_qualified(scope="invented")])
    assert journal.exposure("invocation") == {"scopes": [], "authorities": [], "unknown": False}


def test_corrupt_or_duplicate_json_keys_cannot_reset_exposure(tmp_path):
    directory = tmp_path / "journal"
    journal = _journal(directory)
    journal.record_exposure("invocation", unknown=True)
    record = next(path for path in directory.glob("*.json") if path.name != "binding.json")
    record.write_text('{"value":{},"value":{"unknown":false}}')
    with pytest.raises(MemoryError, match="valid JSON"):
        journal.exposure("invocation")
