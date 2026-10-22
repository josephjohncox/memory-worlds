"""Recoverable context sources and same-scope, dependency-guarded worker artifacts."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from .context_models import (
    ArtifactReceipt,
    ContextBundle,
    ContextBundleInput,
    ContextBundleReceipt,
    ContextSourceRef,
)
from .errors import Conflict, Forbidden, NotFound
from .errors import MemoryError as InvalidRequest
from .maintenance_models import MaintenanceOutput
from .models import EpisodeInput
from .persistence import WorkerLease
from .thread_models import ThreadSource
from .threads import ThreadService

MAX_DECODED = 800_000
CHUNK_SIZE = 64_000


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class ContextArchive:
    def __init__(self, store, threads=None):
        self.store = store
        self.threads = threads or ThreadService(store)

    def _thread(self, principal, thread_id, *, write=False):
        thread = self.threads.get(principal, thread_id)
        with self.store._control() as db:
            self.store._scope_authorized(
                db, principal, thread.world_id, thread.scope_id, "write" if write else "read"
            )
        if write and thread.state != "open":
            raise Conflict("Context archival requires an open unexpired thread")
        return thread

    def _live_refs(self, principal, thread, refs):
        unique = {}
        for value in refs:
            ref = ContextSourceRef.model_validate(value)
            if ref.world_id != thread.world_id or ref.scope_id != thread.scope_id:
                raise Forbidden("Working context and automatic artifacts require the same scope")
            episode = self.store.get_episode(
                principal, ref.world_id, ref.episode_id, scope_id=ref.scope_id
            )
            if episode.digest != ref.digest:
                raise Conflict("Context source no longer matches its frozen digest")
            unique[ref.episode_id] = ref
        return list(unique.values())

    def _flatten_refs(self, principal, thread, refs):
        selected = {ref.episode_id: ref for ref in self._live_refs(principal, thread, refs)}
        if len(selected) > 1000:
            raise Conflict("Derived context exceeds its dependency bound")
        pending = list(selected)
        with self.store._world_db(thread.world_id, thread.scope_id) as db:
            dependencies = self.store._table(db, "artifact_dependencies")
            while pending:
                episode_id = pending.pop()
                for row in db.execute(
                    select(dependencies).where(dependencies.c.output_episode_id == episode_id)
                ).mappings():
                    if row["input_episode_id"] in selected:
                        continue
                    if len(selected) >= 1000:
                        raise Conflict("Derived context exceeds its dependency bound")
                    selected[row["input_episode_id"]] = ContextSourceRef(
                        world_id=thread.world_id,
                        scope_id=thread.scope_id,
                        episode_id=row["input_episode_id"],
                        digest=row["input_digest"],
                    )
                    pending.append(row["input_episode_id"])
        return self._live_refs(principal, thread, selected.values())

    def _capture(self, principal, thread, episode, refs):
        # Source and dependency visibility commit in one scope-journal transaction.
        with self.store._control() as control, self.store._transaction(control):
            self._thread(principal, thread.id, write=True)
            attributed = self.store._capture_principal(
                principal, thread.world_id, str(thread.scope_id), None
            )
            return self._capture_sources(attributed, thread, episode, refs)

    def _capture_sources(self, principal, thread, episode, refs):
        from .lifecycle import visible_clause

        with (
            self.store._world_db(thread.world_id, thread.scope_id) as db,
            self.store._transaction(db),
        ):
            episodes = self.store._table(db, "episodes")
            for ref in refs:
                actual = db.execute(
                    select(episodes.c.digest).where(
                        episodes.c.id == ref.episode_id, visible_clause(self.store, db, episodes)
                    )
                ).scalar()
                if actual != ref.digest:
                    raise Conflict("Context input changed before its source commit")
            receipt = self.store._insert(db, principal, thread.world_id, episode)
            dependencies = self.store._table(db, "artifact_dependencies")
            for ref in refs:
                self.store.persistence.insert_ignore(
                    db,
                    dependencies,
                    {
                        "output_episode_id": receipt.episode.id,
                        "input_episode_id": ref.episode_id,
                        "input_digest": ref.digest,
                    },
                    ["output_episode_id", "input_episode_id"],
                )
        return receipt

    def _bundle_row(self, principal, thread, bundle_id):
        with self.store._control() as db, self.store._transaction(db):
            table = self.store._table(db, "context_bundles")
            row = db.execute(select(table).where(table.c.id == str(bundle_id))).mappings().first()
        if row is None or row["thread_id"] != str(thread.id):
            raise NotFound("Context bundle not found")
        if row["state"] != "ready":
            raise Conflict("Context bundle has no complete durability receipt")
        return row

    def archive(self, principal, thread_id, request: ContextBundleInput) -> ContextBundleReceipt:
        # Take the maintenance boundary before any control/source SQL lock.
        # This bounded synchronous operation performs no provider requests.
        with self.store.persistence.operation_guard():
            return self._archive(principal, thread_id, request)

    def _archive(self, principal, thread_id, request: ContextBundleInput) -> ContextBundleReceipt:
        thread = self._thread(principal, thread_id, write=True)
        request = ContextBundleInput.model_validate(request)
        original = request.model_dump(mode="json")
        requested = digest({"thread_id": str(thread.id), "request": original})
        bid = str(request.operation_id)
        # Only identities, digests and timestamps are persisted in the control
        # intent. The captured payload lives exclusively in its scoped journal.
        with self.store._control() as db, self.store._transaction(db):
            table = self.store._table(db, "context_bundles")
            row = db.execute(select(table).where(table.c.id == bid)).mappings().first()
            if row:
                if row["credential_id"] != principal.credential_id:
                    raise NotFound("Context operation not found")
                if row["request_digest"] != requested:
                    raise Conflict("Context operation already has different input")
                captured_at = row["captured_at"]
            else:
                captured_at = datetime.now(UTC).isoformat()
                db.execute(
                    table.insert().values(
                        id=bid,
                        thread_id=str(thread.id),
                        credential_id=principal.credential_id,
                        request_digest=requested,
                        captured_at=captured_at,
                        state="preparing",
                    )
                )
        if row and row["state"] == "ready":
            return ContextBundleReceipt.model_validate(
                self.bundle(principal, thread.id, bid).model_dump(exclude={"entries", "omissions"})
            )
        ancestors = self._flatten_refs(principal, thread, request.source_refs)
        raw = canonical(original).encode()
        packed = base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
        chunks = [packed[index : index + CHUNK_SIZE] for index in range(0, len(packed), CHUNK_SIZE)]
        refs = []
        for index, chunk in enumerate(chunks):
            receipt = self._capture(
                principal,
                thread,
                EpisodeInput(
                    scope_id=thread.scope_id,
                    source_id=f"context:{bid}:chunk:{index}",
                    content=chunk,
                    occurred_at=captured_at,
                    source_type="text",
                    session_id=str(thread.id),
                    source_entry_id=f"{bid}:{index}",
                    metadata={"context_bundle_id": bid, "chunk": index, "codec": "gzip-base64"},
                    project=False,
                ),
                ancestors,
            )
            refs.append(
                ContextSourceRef(
                    world_id=thread.world_id,
                    scope_id=thread.scope_id,
                    episode_id=receipt.episode.id,
                    digest=receipt.episode.digest,
                )
            )
        manifest = {
            "bundle_id": bid,
            "thread_id": str(thread.id),
            "digest": hashlib.sha256(raw).hexdigest(),
            "chunks": [ref.model_dump(mode="json") for ref in refs],
            "uncompressed_bytes": len(raw),
            "stored_bytes": len(packed),
        }
        receipt = self._capture(
            principal,
            thread,
            EpisodeInput(
                scope_id=thread.scope_id,
                source_id=f"context:{bid}:manifest",
                content=canonical(manifest),
                occurred_at=captured_at,
                source_type="text",
                session_id=str(thread.id),
                source_entry_id=bid,
                metadata={"context_bundle_id": bid},
                project=False,
            ),
            ancestors,
        )
        refs.append(
            ContextSourceRef(
                world_id=thread.world_id,
                scope_id=thread.scope_id,
                episode_id=receipt.episode.id,
                digest=receipt.episode.digest,
            )
        )
        revision = self.threads.append(
            principal,
            thread.id,
            request.operation_id,
            request.expected_revision,
            [ThreadSource(episode_id=ref.episode_id, digest=ref.digest) for ref in refs],
        )
        manifest["source_refs"] = [ref.model_dump(mode="json") for ref in refs]
        with self.store._control() as db, self.store._transaction(db):
            table = self.store._table(db, "context_bundles")
            db.execute(
                table.update()
                .where(table.c.id == bid)
                .values(
                    state="ready",
                    manifest=canonical(manifest),
                    revision=revision.revision,
                )
            )
        return ContextBundleReceipt.model_validate(
            self.bundle(principal, thread.id, bid).model_dump(exclude={"entries", "omissions"})
        )

    def bundle(self, principal, thread_id, bundle_id) -> ContextBundle:
        thread = self._thread(principal, thread_id)
        row = self._bundle_row(principal, thread, bundle_id)
        manifest = json.loads(row["manifest"])
        self._live_refs(principal, thread, manifest["source_refs"])
        parts = []
        for ref in manifest["chunks"]:
            episode = self.store.get_episode(
                principal, thread.world_id, ref["episode_id"], scope_id=thread.scope_id
            )
            parts.append(episode.content)
        try:
            compressed = base64.b64decode("".join(parts), validate=True)
            with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
                raw = stream.read(MAX_DECODED + 1)
            if len(raw) > MAX_DECODED or len(raw) != manifest["uncompressed_bytes"]:
                raise ValueError
            if hashlib.sha256(raw).hexdigest() != manifest["digest"]:
                raise ValueError
            original = ContextBundleInput.model_validate_json(raw)
        except Exception:
            raise Conflict("Context archive failed its bounded integrity check") from None
        self._live_refs(principal, thread, original.source_refs)
        return ContextBundle(
            id=UUID(row["id"]),
            bundle_id=UUID(row["id"]),
            thread_id=thread.id,
            revision=row["revision"],
            digest=manifest["digest"],
            coverage="partial" if original.omissions else "complete",
            source_refs=manifest["source_refs"],
            entries=original.entries,
            omissions=original.omissions,
            uncompressed_bytes=manifest["uncompressed_bytes"],
            stored_bytes=manifest["stored_bytes"],
        )

    def load_inputs(self, principal, thread_id, bundle_ids):
        thread = self._thread(principal, thread_id)
        refs, manifest, payloads, total_bytes = {}, [], [], 2
        for bid in bundle_ids:
            bundle = self.bundle(principal, thread.id, bid)
            payload = canonical(
                {
                    "bundle_id": str(bundle.id),
                    "coverage": bundle.coverage,
                    "entries": [entry.model_dump(mode="json") for entry in bundle.entries],
                    "omissions": [
                        omission.model_dump(mode="json") for omission in bundle.omissions
                    ],
                }
            )
            total_bytes += len(payload.encode()) + bool(payloads)
            if total_bytes > 1_048_576:
                raise InvalidRequest("Selected maintenance context exceeds 1 MiB")
            payloads.append(payload)
            manifest.append({"id": str(bundle.id), "digest": bundle.digest})
            for ref in bundle.source_refs:
                refs[ref.episode_id] = ref.model_dump(mode="json")
            # Explicit captured source provenance remains part of derivation.
            for ref in self._captured_refs(principal, thread, bundle):
                refs[ref.episode_id] = ref.model_dump(mode="json")
            if len(refs) > 1000:
                raise InvalidRequest("Selected maintenance context exceeds its dependency bound")
        # Freeze and bound complete lineage before any paid inference; a small
        # direct selection may reference a long chain of previous summaries.
        expanded = self._flatten_refs(principal, thread, refs.values())
        return {
            "manifest": manifest,
            "source_refs": [ref.model_dump(mode="json") for ref in expanded],
            "text": "[" + ",".join(payloads) + "]",
        }

    def authorize_job(self, principal, thread_id, operation):
        if operation not in {"submit", "dispatch", "read"}:
            raise Forbidden("Unknown maintenance operation")
        self._thread(principal, thread_id, write=operation != "read")

    def _captured_refs(self, principal, thread, bundle):
        row = self._bundle_row(principal, thread, bundle.id)
        manifest = json.loads(row["manifest"])
        packed = "".join(
            self.store.get_episode(
                principal, thread.world_id, ref["episode_id"], scope_id=thread.scope_id
            ).content
            for ref in manifest["chunks"]
        )
        with gzip.GzipFile(fileobj=io.BytesIO(base64.b64decode(packed, validate=True))) as stream:
            raw = stream.read(MAX_DECODED + 1)
        original = ContextBundleInput.model_validate_json(raw)
        return self._live_refs(principal, thread, original.source_refs)

    def write_artifact(self, principal, job_id, frozen_input, output):
        # fenced() also serves lease heartbeats and intentionally omits the
        # maintenance guard. Source effects must acquire it before that fence.
        with self.store.persistence.operation_guard():
            return self._write_artifact(principal, job_id, frozen_input, output)

    def _write_artifact(self, principal, job_id, frozen_input, output):
        thread = self._thread(principal, frozen_input["thread_id"], write=True)
        output = MaintenanceOutput.model_validate(output)
        if {str(b) for b in output.source_bundle_ids} != set(frozen_input["bundle_ids"]):
            raise Conflict("Worker output changed its source bundle selection")
        refs = self._flatten_refs(principal, thread, frozen_input["source_refs"])
        artifact_id = UUID(str(frozen_input["result_artifact_id"]))
        frozen = {key: value for key, value in frozen_input.items() if not key.startswith("_")}
        request_digest = digest(frozen)
        with self.store._control() as db, self.store._transaction(db):
            table = self.store._table(db, "artifact_locations")
            row = db.execute(select(table).where(table.c.id == str(artifact_id))).mappings().first()
            if row:
                if row["request_digest"] != request_digest or row["job_id"] != str(job_id):
                    raise Conflict("Artifact identity already has another frozen intent")
                occurred_at = row["created_at"]
            else:
                occurred_at = datetime.now(UTC).isoformat()
                db.execute(
                    table.insert().values(
                        id=str(artifact_id),
                        thread_id=str(thread.id),
                        job_id=str(job_id),
                        request_digest=request_digest,
                        created_at=occurred_at,
                    )
                )
        episode = EpisodeInput(
            scope_id=thread.scope_id,
            source_id=f"maintenance:artifact:{artifact_id}",
            content=output.content,
            occurred_at=occurred_at,
            source_type="text",
            session_id=str(thread.id),
            role="assistant",
            metadata={
                "maintenance_artifact_id": str(artifact_id),
                "kind": output.kind,
                "job_id": str(job_id),
                "model_profile_digest": frozen.get("profile_digest"),
                "claim_status": "tentative",
                **output.model_dump(mode="json", exclude={"content"}),
            },
            project=True,
        )
        # Acquire no SQL lease transaction across provider calls. The callback is
        # synchronous; source checks, output and dependencies commit together.
        lease = frozen_input.get("_lease")
        if not isinstance(lease, WorkerLease) or lease.resource != f"maintenance:{job_id}":
            raise Conflict("Artifact commit requires a live maintenance worker fence")
        with self.store.persistence.fenced(lease):
            self._thread(principal, thread.id, write=True)
            attributed = self.store._capture_principal(
                principal, thread.world_id, str(thread.scope_id), None
            )
            with (
                self.store._world_db(thread.world_id, thread.scope_id) as db,
                self.store._transaction(db),
            ):
                from .lifecycle import visible_clause

                episodes = self.store._table(db, "episodes")
                for ref in refs:
                    actual = db.execute(
                        select(episodes.c.digest).where(
                            episodes.c.id == ref.episode_id,
                            visible_clause(self.store, db, episodes),
                        )
                    ).scalar()
                    if actual != ref.digest:
                        raise Conflict("Worker source was removed before artifact commit")
                receipt = self.store._insert(db, attributed, thread.world_id, episode)
                dependencies = self.store._table(db, "artifact_dependencies")
                for ref in refs:
                    self.store.persistence.insert_ignore(
                        db,
                        dependencies,
                        {
                            "output_episode_id": receipt.episode.id,
                            "input_episode_id": ref.episode_id,
                            "input_digest": ref.digest,
                        },
                        ["output_episode_id", "input_episode_id"],
                    )
                artifacts = self.store._table(db, "context_artifacts")
                self.store.persistence.insert_ignore(
                    db,
                    artifacts,
                    {
                        "id": str(artifact_id),
                        "episode_id": receipt.episode.id,
                        "thread_id": str(thread.id),
                        "job_id": str(job_id),
                        "kind": output.kind,
                        "source_refs": canonical([ref.model_dump(mode="json") for ref in refs]),
                    },
                    ["id"],
                )
        return self.artifact(principal, artifact_id).model_dump(mode="json")

    def artifact(self, principal, artifact_id) -> ArtifactReceipt:
        with self.store._control() as db:
            table = self.store._table(db, "artifact_locations")
            location = (
                db.execute(select(table).where(table.c.id == str(artifact_id))).mappings().first()
            )
        if location is None:
            raise NotFound("Maintenance artifact not found")
        thread = self._thread(principal, location["thread_id"])
        with self.store._world_db(thread.world_id, thread.scope_id) as db:
            table = self.store._table(db, "context_artifacts")
            row = db.execute(select(table).where(table.c.id == str(artifact_id))).mappings().first()
        if row is None:
            raise NotFound("Maintenance artifact has no accepted payload")
        refs = self._live_refs(principal, thread, json.loads(row["source_refs"]))
        episode = self.store.get_episode(
            principal, thread.world_id, row["episode_id"], scope_id=thread.scope_id
        )
        return ArtifactReceipt(
            id=UUID(str(artifact_id)),
            artifact_id=UUID(str(artifact_id)),
            thread_id=thread.id,
            job_id=UUID(row["job_id"]),
            kind=row["kind"],
            content=episode.content,
            metadata=episode.metadata,
            source_refs=refs,
        )
