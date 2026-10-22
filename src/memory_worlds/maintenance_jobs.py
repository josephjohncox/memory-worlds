"""Durable, fenced maintenance proposals over exact authorized input manifests.

Control rows retain identities/configuration only. Source text and proposal bodies
live exclusively behind the dependency-aware source callbacks. A model cannot
select destinations, grant access, publish, or delete sources.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

from sqlalchemy import and_, or_, select

from .errors import Conflict, Forbidden, NotFound, Unauthorized, Unavailable
from .errors import MemoryError as InvalidRequest
from .maintenance_models import MaintenanceReceipt, MaintenanceRequest
from .maintenance_provider import (
    OpenAICompatibleWorker,
    ProviderError,
    WorkerProfile,
    WorkerProfiles,
    canonical,
    checked_output,
    digest,
    provider_payload,
)
from .models import Principal
from .persistence import LeaseLost, WorkerLease

_TERMINAL = {"succeeded", "failed", "cancelled", "blocked", "stale"}


def _now():
    return datetime.now(UTC).isoformat()


async def _call(callback, *args):
    result = callback(*args)
    return await result if inspect.isawaitable(result) else result


def _metadata(value, request: MaintenanceRequest):
    """Allow only bounded qualified source identities into the control journal."""
    try:
        manifest = []
        for item in value["manifest"]:
            if set(item) != {"id", "digest"}:
                raise ValueError
            identifier = str(UUID(str(item["id"])))
            checksum = item["digest"]
            if not isinstance(checksum, str) or len(checksum) != 64:
                raise ValueError
            int(checksum, 16)
            manifest.append({"id": identifier, "digest": checksum})
        if len(manifest) != len(request.bundle_ids) or {item["id"] for item in manifest} != {
            str(item) for item in request.bundle_ids
        }:
            raise ValueError
        source_refs = value["source_refs"]
        if not isinstance(source_refs, list) or len(source_refs) > 10_000:
            raise ValueError
        refs = []
        for item in source_refs:
            if set(item) != {"world_id", "scope_id", "episode_id", "digest"}:
                raise ValueError
            if any(not isinstance(item[key], str) for key in item):
                raise ValueError
            if not 1 <= len(item["world_id"]) <= 100 or not 1 <= len(item["episode_id"]) <= 100:
                raise ValueError
            UUID(item["scope_id"])
            if len(item["digest"]) != 64:
                raise ValueError
            int(item["digest"], 16)
            refs.append(dict(item))
        text = value["text"]
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 1_048_576:
            raise ValueError
        return {"manifest": manifest, "source_refs": refs}, text
    except (ValueError, TypeError, KeyError):
        raise InvalidRequest(
            "Maintenance input manifest is invalid or exceeds its bounds"
        ) from None


class MaintenanceJobs:
    def __init__(
        self,
        store,
        *,
        input_loader: Callable,
        output_writer: Callable,
        result_reader: Callable,
        authorize_job: Callable,
        profiles: WorkerProfiles | None = None,
        provider=None,
        lease_ttl: float = 30,
    ):
        self.store = store
        self.input_loader = input_loader
        self.output_writer = output_writer
        self.result_reader = result_reader
        self.authorize_job = authorize_job
        self.profiles = profiles if profiles is not None else WorkerProfiles.from_env()
        self.provider = provider if provider is not None else OpenAICompatibleWorker()
        self.lease_ttl = lease_ttl
        self.worker_id = uuid4().hex
        self._closed = False
        self._wake = asyncio.Event()

    def _row(self, job_id):
        with self.store._control() as db:
            table = self.store._table(db, "maintenance_jobs")
            row = db.execute(select(table).where(table.c.id == str(job_id))).mappings().first()
            if row is None:
                raise NotFound("Maintenance job not found")
            return dict(row)

    def _owned(self, principal, job_id):
        self.store.validate_principal(principal)
        row = self._row(job_id)
        if row["user_id"] != principal.user_id:
            raise NotFound("Maintenance job not found")
        return row

    async def _load(self, principal, request, *, operation="read"):
        self.store.validate_principal(principal)
        await _call(self.authorize_job, principal, request.thread_id, operation)
        value = await _call(self.input_loader, principal, request.thread_id, request.bundle_ids)
        self.store.validate_principal(principal)
        return _metadata(value, request)

    def _profile(self, principal, request, metadata):
        profile = self.profiles.profiles.get(request.profile)
        if profile is None:
            return None
        if profile.allowed_user_ids and principal.user_id not in profile.allowed_user_ids:
            raise Forbidden("Maintenance profile is not authorized")
        if profile.allowed_world_ids and any(
            item["world_id"] not in profile.allowed_world_ids for item in metadata["source_refs"]
        ):
            raise Forbidden("Maintenance profile is not permitted for these inputs")
        return profile

    async def submit(self, principal, request: MaintenanceRequest, *, wait_seconds: float = 0):
        request = MaintenanceRequest.model_validate(request)
        self.store.validate_principal(principal)
        request_json = request.model_dump(mode="json")
        checksum = digest(request_json)
        try:
            old = self._owned(principal, request.operation_id)
        except NotFound:
            old = None
        if old is not None:
            if old["credential_id"] != principal.credential_id:
                raise Forbidden("Maintenance replay requires the submitting credential")
            if old["request_digest"] != checksum:
                raise Conflict("Maintenance operation ID already has a different request")
            result = await self.wait(principal, request.operation_id, wait_seconds=wait_seconds)
            return result.model_copy(update={"replayed": True})
        metadata, _text = await self._load(principal, request, operation="submit")
        profile = self._profile(principal, request, metadata)
        frozen = {
            **metadata,
            "thread_id": str(request.thread_id),
            "bundle_ids": [str(item) for item in request.bundle_ids],
            "kind": request.kind,
            "profile_digest": profile.configuration_digest() if profile else None,
            "result_artifact_id": str(uuid5(request.operation_id, "maintenance-result")),
        }
        with self.store._control() as db:
            self.store._principal(db, principal)
            table = self.store._table(db, "maintenance_jobs")
            self.store.persistence.insert_ignore(
                db,
                table,
                {
                    "id": str(request.operation_id),
                    "credential_id": principal.credential_id,
                    "user_id": principal.user_id,
                    "principal": principal.model_dump_json(),
                    "request": canonical(request_json),
                    "request_digest": checksum,
                    "frozen_input": canonical(frozen),
                    "profile": canonical(profile.snapshot()) if profile else None,
                    "profile_digest": frozen["profile_digest"],
                    "state": "queued" if profile else "blocked",
                    "attempts": 0,
                    "cancel_requested": 0,
                    "effect_dispatched": 0,
                    "error": None if profile else "model_profile_unavailable",
                    "next_attempt": 0,
                    "created_at": _now(),
                    "updated_at": _now(),
                },
                ["id"],
            )
            current = db.execute(select(table).where(table.c.id == str(request.operation_id)))
            current = current.mappings().one()
            if current["credential_id"] != principal.credential_id:
                raise Forbidden("Maintenance operation belongs to another credential")
            if current["request_digest"] != checksum:
                raise Conflict("Maintenance operation ID already has a different request")
        self._wake.set()
        return await self.wait(principal, request.operation_id, wait_seconds=wait_seconds)

    async def status(self, principal, job_id):
        row = self._owned(principal, job_id)
        request = MaintenanceRequest.model_validate_json(row["request"])
        await self._load(principal, request)
        result = None
        if row["result_artifact_id"]:
            result = await _call(self.result_reader, principal, UUID(row["result_artifact_id"]))
            if hasattr(result, "model_dump"):
                result = result.model_dump(mode="json")
        with self.store._control() as db:
            attempts = self.store._table(db, "maintenance_attempts")
            unknown = list(
                db.execute(
                    select(attempts.c.number).where(
                        attempts.c.job_id == row["id"], attempts.c.state == "unknown"
                    )
                ).scalars()
            )
        unresolved = [f"provider:{number}" for number in unknown]
        if row["effect_dispatched"] and not row["result_artifact_id"]:
            unresolved.append("artifact")
        return MaintenanceReceipt(
            id=row["id"],
            operation_id=row["id"],
            thread_id=request.thread_id,
            kind=request.kind,
            profile=request.profile,
            state=row["state"],
            attempts=row["attempts"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            error=row["error"],
            result_artifact_id=row["result_artifact_id"],
            result=result,
            usage_uncertain=bool(unknown),
            unresolved_effects=unresolved,
        )

    async def list(self, principal, *, limit=50):
        self.store.validate_principal(principal)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise InvalidRequest("Maintenance list limit must be between one and 100")
        with self.store._control() as db:
            table = self.store._table(db, "maintenance_jobs")
            ids = list(
                db.execute(
                    select(table.c.id)
                    .where(table.c.user_id == principal.user_id)
                    .order_by(table.c.created_at.desc())
                    .limit(limit)
                ).scalars()
            )
        results = []
        for identifier in ids:
            try:
                results.append(await self.status(principal, identifier))
            except (Forbidden, Unauthorized, NotFound):
                continue
        return results

    async def wait(self, principal, job_id, *, wait_seconds=30):
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)):
            raise InvalidRequest("Maintenance wait deadline must be between zero and 60 seconds")
        if not 0 <= wait_seconds <= 60:
            raise InvalidRequest("Maintenance wait deadline must be between zero and 60 seconds")
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            receipt = await self.status(principal, job_id)
            remaining = deadline - asyncio.get_running_loop().time()
            if receipt.state in _TERMINAL or remaining <= 0:
                return receipt
            await asyncio.sleep(min(0.05, remaining))

    async def cancel(self, principal, job_id):
        row = self._owned(principal, job_id)
        if row["credential_id"] != principal.credential_id:
            raise Forbidden("Maintenance cancellation requires the submitting credential")
        request = MaintenanceRequest.model_validate_json(row["request"])
        await self._load(principal, request)
        with self.store._control() as db:
            self.store._principal(db, principal)
            table = self.store._table(db, "maintenance_jobs")
            db.execute(
                table.update()
                .where(
                    table.c.id == row["id"], table.c.state.not_in(["succeeded", "failed", "stale"])
                )
                .values(cancel_requested=1, state="cancelled", updated_at=_now())
            )
        return await self.status(principal, job_id)

    def _lease(self, row):
        return WorkerLease(
            "maintenance:" + row["id"],
            row["lease_owner"],
            row["lease_fence"],
            row["lease_expires_at"],
        )

    def _claim(self, job_id):
        lease = self.store.persistence.acquire_lease(
            "maintenance:" + str(job_id), self.worker_id, ttl=self.lease_ttl
        )
        if lease is None:
            return None
        claimed = False
        try:
            with (
                self.store.persistence.operation_guard(),
                self.store.persistence.fenced(lease) as db,
                self.store.persistence.transaction(db),
            ):
                table = self.store._table(db, "maintenance_jobs")
                row = db.execute(select(table).where(table.c.id == str(job_id))).mappings().one()
                now = self.store.persistence.database_time(db)
                if row["state"] not in {"queued", "running"} or row["next_attempt"] > now:
                    return None
                profile = WorkerProfile.model_validate_json(row["profile"])
                leases = self.store._table(db, "worker_leases")
                running = list(
                    db.execute(
                        select(table.c.id)
                        .join(
                            leases,
                            leases.c.resource == "maintenance:" + table.c.id,
                        )
                        .where(
                            table.c.profile_digest == row["profile_digest"],
                            leases.c.expires_at > now,
                            table.c.id != row["id"],
                        )
                    ).scalars()
                )
                if len(running) >= profile.max_concurrent_jobs:
                    return None
                db.execute(
                    table.update()
                    .where(table.c.id == row["id"])
                    .values(
                        state="running",
                        lease_owner=lease.owner,
                        lease_fence=lease.fence,
                        lease_expires_at=lease.expires_at,
                        updated_at=_now(),
                    )
                )
                claimed = True
                return {
                    **dict(row),
                    "lease_owner": lease.owner,
                    "lease_fence": lease.fence,
                    "lease_expires_at": lease.expires_at,
                }
        finally:
            if not claimed:
                self.store.persistence.release_lease(lease)

    def _update(self, row, **values):
        with (
            self.store.persistence.operation_guard(),
            self.store.persistence.fenced(self._lease(row)) as db,
            self.store.persistence.transaction(db),
        ):
            table = self.store._table(db, "maintenance_jobs")
            current = db.execute(select(table).where(table.c.id == row["id"])).mappings().one()
            if current["cancel_requested"]:
                values["state"] = "cancelled"
            db.execute(
                table.update().where(table.c.id == row["id"]).values(**values, updated_at=_now())
            )

    def _dispatch(self, row, profile):
        """Cancel and inference dispatch serialize through one short control commit."""
        with (
            self.store.persistence.operation_guard(),
            self.store.persistence.fenced(self._lease(row)) as db,
            self.store.persistence.transaction(db),
        ):
            table = self.store._table(db, "maintenance_jobs")
            current = db.execute(select(table).where(table.c.id == row["id"])).mappings().one()
            if current["cancel_requested"]:
                return None
            number = current["attempts"] + 1
            if number > profile.max_attempts:
                db.execute(
                    table.update()
                    .where(table.c.id == row["id"])
                    .values(state="failed", error="attempt_limit_exceeded", updated_at=_now())
                )
                return None
            budgets = self.store._table(db, "maintenance_budgets")
            day = (
                datetime.fromtimestamp(self.store.persistence.database_time(db), UTC)
                .date()
                .isoformat()
            )
            key = {"profile_digest": row["profile_digest"], "day": day}
            self.store.persistence.insert_ignore(
                db, budgets, {**key, "reserved_tokens": 0}, list(key)
            )
            where = and_(budgets.c.profile_digest == key["profile_digest"], budgets.c.day == day)
            reserved = db.execute(select(budgets.c.reserved_tokens).where(where)).scalar_one()
            amount = profile.max_input_tokens + profile.max_output_tokens
            if reserved + amount > profile.daily_token_budget:
                db.execute(
                    table.update()
                    .where(table.c.id == row["id"])
                    .values(state="blocked", error="daily_token_budget_exceeded", updated_at=_now())
                )
                return None
            db.execute(budgets.update().where(where).values(reserved_tokens=reserved + amount))
            attempts = self.store._table(db, "maintenance_attempts")
            db.execute(
                attempts.insert().values(
                    job_id=row["id"],
                    number=number,
                    state="unknown",
                    reserved_tokens=amount,
                    dispatched_at=_now(),
                )
            )
            db.execute(
                table.update()
                .where(table.c.id == row["id"])
                .values(attempts=number, updated_at=_now(), error=None)
            )
            return number

    def _acknowledge(self, row, number, *, error=None):
        with (
            self.store.persistence.operation_guard(),
            self.store.persistence.fenced(self._lease(row)) as db,
            self.store.persistence.transaction(db),
        ):
            attempts = self.store._table(db, "maintenance_attempts")
            db.execute(
                attempts.update()
                .where(
                    attempts.c.job_id == row["id"],
                    attempts.c.number == number,
                )
                .values(state="acknowledged", error=error)
            )

    async def _inputs_current(self, principal, request, frozen):
        metadata, text = await self._load(principal, request, operation="dispatch")
        if any(metadata[key] != frozen[key] for key in ("manifest", "source_refs")):
            raise Conflict("Maintenance input revision changed")
        return metadata, text

    async def _reconcile(self, principal, row, frozen):
        try:
            result = await _call(self.result_reader, principal, UUID(frozen["result_artifact_id"]))
        except NotFound:
            return False
        self._check_result(result, frozen)
        self._update(
            row, result_artifact_id=frozen["result_artifact_id"], state="succeeded", error=None
        )
        return True

    @staticmethod
    def _check_result(result, frozen):
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        try:
            identifier = str(result.get("artifact_id", result.get("id")))
            if identifier != frozen["result_artifact_id"]:
                raise ValueError
        except (TypeError, AttributeError, ValueError):
            raise Conflict(
                "Maintenance artifact receipt does not match its planned identity"
            ) from None

    async def _process(self, row):
        try:
            await self._process_owned(row)
        except (LeaseLost, Unavailable):
            # Ownership can expire or maintenance can begin during failure recording.
            return

    async def _process_owned(self, row):
        lease = self._lease(row)
        principal = Principal.model_validate_json(row["principal"])
        request = MaintenanceRequest.model_validate_json(row["request"])
        frozen = json.loads(row["frozen_input"])
        profile = WorkerProfile.model_validate_json(row["profile"])
        number = None
        try:
            async with self.store.persistence.maintain_lease(
                lease, ttl=self.lease_ttl
            ) as assert_live:
                metadata, text = await self._inputs_current(principal, request, frozen)
                assert_live()
                if await self._reconcile(principal, row, frozen):
                    return
                current_profile = self._profile(principal, request, metadata)
                if (
                    current_profile is None
                    or current_profile.configuration_digest() != row["profile_digest"]
                ):
                    self._update(row, state="blocked", error="model_profile_changed")
                    return
                # Preflight complete request bounds before reserving any dispatch.
                provider_payload(
                    profile, kind=request.kind, text=text, bundle_ids=request.bundle_ids
                )
                await self._inputs_current(principal, request, frozen)
                self.store.validate_principal(principal)
                assert_live()
                number = self._dispatch(row, profile)
                if number is None:
                    return
                async with asyncio.timeout(profile.timeout_seconds):
                    output = await self.provider.propose(
                        profile, kind=request.kind, text=text, bundle_ids=request.bundle_ids
                    )
                assert_live()
                self._acknowledge(row, number)
                output = checked_output(output, kind=request.kind, bundle_ids=request.bundle_ids)
                await self._inputs_current(principal, request, frozen)
                self.store.validate_principal(principal)
                assert_live()
                with (
                    self.store.persistence.operation_guard(),
                    self.store.persistence.fenced(lease) as db,
                    self.store.persistence.transaction(db),
                ):
                    table = self.store._table(db, "maintenance_jobs")
                    current = (
                        db.execute(select(table).where(table.c.id == row["id"])).mappings().one()
                    )
                    if current["cancel_requested"]:
                        return
                    db.execute(
                        table.update()
                        .where(table.c.id == row["id"])
                        .values(effect_dispatched=1, updated_at=_now())
                    )
                result = await _call(
                    self.output_writer,
                    principal,
                    request.operation_id,
                    {**frozen, "_lease": lease},
                    output,
                )
                assert_live()
                self._check_result(result, frozen)
                self._update(
                    row,
                    result_artifact_id=frozen["result_artifact_id"],
                    state="succeeded",
                    error=None,
                )
        except LeaseLost:
            return
        except Unavailable:
            # The backup/maintenance cut excludes all new semantic mutations.
            # Operational release remains allowed; recovery retains any unknown attempt.
            return
        except asyncio.CancelledError:
            # Leave a dispatched attempt unknown; another fenced owner reconciles it.
            try:
                self._update(row, state="queued", error="worker_interrupted")
            except (LeaseLost, Unavailable):
                pass
            raise
        except (Forbidden, Unauthorized):
            self._update(row, state="blocked", error="authorization_changed")
        except (Conflict, NotFound):
            self._update(row, state="stale", error="input_or_dependency_changed")
        except ProviderError as error:
            if number is not None and not error.usage_uncertain:
                self._acknowledge(row, number, error=error.code)
            state = (
                "queued" if error.retryable and (number or 0) < profile.max_attempts else "failed"
            )
            with self.store._control() as db:
                retry_at = self.store.persistence.database_time(db) + min(30, 2 ** (number or 0))
            self._update(row, state=state, error=error.code, next_attempt=retry_at)
        except TimeoutError:
            self._update(
                row,
                state="queued" if (number or 0) < profile.max_attempts else "failed",
                error="provider_timeout",
            )
        except InvalidRequest:
            self._update(row, state="failed", error="invalid_maintenance_input")
        except Exception:
            # Includes lost source-artifact acknowledgement. Retry reconciles its fixed ID.
            self._update(
                row,
                state="queued" if number is not None else "failed",
                error="maintenance_execution_failed",
            )
        finally:
            self.store.persistence.release_lease(lease)

    async def run_once(self, job_id: UUID | None = None):
        with self.store._control() as db:
            table = self.store._table(db, "maintenance_jobs")
            now = self.store.persistence.database_time(db)
            query = select(table.c.id).where(
                or_(
                    table.c.state == "queued",
                    and_(table.c.state == "running", table.c.lease_expires_at <= now),
                ),
                table.c.next_attempt <= now,
            )
            if job_id is not None:
                query = query.where(table.c.id == str(job_id))
            ids = list(db.execute(query.order_by(table.c.created_at).limit(100)).scalars())
        claimed = [row for identifier in ids if (row := self._claim(identifier)) is not None]
        # Claim-side per-profile limits are database serialized across processes.
        await asyncio.gather(*(self._process(row) for row in claimed))
        return len(claimed)

    async def run(self):
        while not self._closed:
            self._wake.clear()
            try:
                await self.run_once()
            except Unavailable:
                pass
            try:
                await asyncio.wait_for(self._wake.wait(), self.profiles.poll_seconds)
            except TimeoutError:
                pass

    async def close(self):
        self._closed = True
        self._wake.set()
        close = getattr(self.provider, "close", None)
        if close:
            await _call(close)
