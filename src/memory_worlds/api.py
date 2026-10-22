import asyncio
import re
import time
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from . import __version__
from .auth import AuthConfig, OIDCAuthenticator
from .context_archive import ContextArchive
from .context_models import ArtifactReceipt, ContextBundle, ContextBundleInput, ContextBundleReceipt
from .errors import MemoryError, Unauthorized, Unavailable
from .identity_names import (
    CreateIdentity,
    IdentityKind,
    IdentityRegistry,
    NamedIdentity,
    UpdateIdentity,
)
from .lifecycle import EpisodeLifecycle, LifecycleRequest, RetentionEngine, RetentionRequest
from .locking import DaemonLock
from .maintenance_jobs import MaintenanceJobs
from .maintenance_models import MaintenanceReceipt, MaintenanceRequest
from .metrics import Metrics
from .models import (
    MAX_IMPORT_BYTES,
    AgentContext,
    BindContext,
    CreateScope,
    CreateToken,
    CreateWorld,
    DelegateCredential,
    Episode,
    EpisodeInput,
    ImportBatch,
    MembershipUpdate,
    Principal,
    Publication,
    Receipt,
    Scope,
    ScopedPublication,
    ScopeFlow,
    SearchRequest,
    UpdateAlias,
    UpdateScopeGrant,
    UserUpdate,
    World,
)
from .mounts import MountConfig, MountView
from .operations import BackupRequest, BackupService
from .outbox import Delivery, RoutedPublication, RouterSearch
from .router import WorldRouter
from .store import MemoryStore
from .thread_models import CreateThread, Thread
from .threads import ThreadService

bearer = HTTPBearer(auto_error=False)
_IMPORT_ROUTE = re.compile(
    r"/v1/worlds/[a-zA-Z0-9_-]{1,100}/scopes/"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/imports"
)


def create_app(
    data_dir: Path,
    graph=None,
    *,
    router=None,
    auth=None,
    mount_config=None,
    store=None,
    backups=None,
    maintenance=None,
    maintenance_profiles=None,
    maintenance_provider=None,
) -> FastAPI:
    store = store or MemoryStore.from_env(data_dir)
    identities = IdentityRegistry(store)
    lifecycle, retention = EpisodeLifecycle(store), RetentionEngine(store)
    backups = backups or BackupService.from_env(store)
    metrics = Metrics()
    if auth is None:
        config = AuthConfig.from_env()
        auth = OIDCAuthenticator(store, config) if config else None
    router = router or WorldRouter(store, mount_config or MountConfig.from_env())
    threads = ThreadService(store)
    context_archive = ContextArchive(store, threads)
    maintenance = maintenance or MaintenanceJobs(
        store,
        input_loader=context_archive.load_inputs,
        output_writer=context_archive.write_artifact,
        result_reader=context_archive.artifact,
        authorize_job=context_archive.authorize_job,
        profiles=maintenance_profiles,
        provider=maintenance_provider,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with nullcontext() if store.persistence.is_postgres else DaemonLock(data_dir):
            store.reconcile_oidc_issuer(auth.config.issuer if auth is not None else None)
            if auth is not None:
                auth.reconcile()
            tasks = []
            if graph is not None:
                tasks.append(asyncio.create_task(graph.run(), name="graph-projection"))
            if router.mounts:
                tasks.append(asyncio.create_task(router.run(), name="publication-outbox"))
            if maintenance.profiles.profiles:
                tasks.append(asyncio.create_task(maintenance.run(), name="memory-maintenance"))
            app.state.workers = tasks
            app.state.started = True
            try:
                yield
            finally:
                app.state.started = False
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if graph is not None:
                    await graph.close()
                await maintenance.close()

    app = FastAPI(title="Memory Worlds", version=__version__, lifespan=lifespan)
    app.state.store = store
    app.state.router = router
    app.state.threads = threads
    app.state.context_archive = context_archive
    app.state.maintenance = maintenance
    app.state.started = False
    app.state.workers = []

    @app.middleware("http")
    async def measured_request(request: Request, call_next):
        started, status = time.perf_counter(), 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            metrics.observe(request.method, status, time.perf_counter() - started)

    @app.middleware("http")
    async def bounded_body(request: Request, call_next):
        chunks = []
        size = 0
        maximum = (
            MAX_IMPORT_BYTES
            if request.method == "POST" and _IMPORT_ROUTE.fullmatch(request.scope["path"])
            else 1_048_576
        )
        async for chunk in request.stream():
            size += len(chunk)
            if size > maximum:
                return JSONResponse(status_code=413, content={"error": "request_too_large"})
            chunks.append(chunk)
        request._body = b"".join(chunks)
        return await call_next(request)

    @app.exception_handler(MemoryError)
    async def memory_error(_request: Request, exc: MemoryError):
        return JSONResponse(
            status_code=exc.status,
            content={"error": exc.code, "detail": str(exc)},
            headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        # Avoid echoing transcripts or credentials in error bodies and logs.
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "detail": [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()],
            },
        )

    def principal(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Principal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise Unauthorized("a bearer credential is required")
        token = credentials.credentials
        if token.startswith("mw_"):
            return store.validate_principal(store.authenticate(token))
        if auth is not None:
            return auth.authenticate(token)
        raise Unauthorized("credential is invalid or revoked")

    User = Annotated[Principal, Depends(principal)]

    @app.post("/v1/threads", response_model=Thread)
    def create_thread(body: CreateThread, user: User):
        return threads.create(user, body)

    @app.get("/v1/threads", response_model=list[Thread])
    def list_threads(user: User, world_id: str | None = None, include_expired: bool = False):
        if world_id and world_id.startswith("@"):
            matches = [world.id for world in store.list_worlds(user) if world.alias == world_id[1:]]
            if len(matches) != 1:
                from .errors import NotFound

                raise NotFound("No visible world matches that alias")
            world_id = matches[0]
        return threads.list(user, world_id=world_id, include_expired=include_expired)

    @app.get("/v1/threads/{thread_id}", response_model=Thread)
    def get_thread(thread_id: UUID, user: User):
        return threads.get(user, thread_id)

    @app.post("/v1/threads/{thread_id}/bundles", response_model=ContextBundleReceipt)
    def archive_context(thread_id: UUID, body: ContextBundleInput, user: User):
        return context_archive.archive(user, thread_id, body)

    @app.get("/v1/threads/{thread_id}/bundles/{bundle_id}", response_model=ContextBundle)
    def get_context_bundle(thread_id: UUID, bundle_id: UUID, user: User):
        return context_archive.bundle(user, thread_id, bundle_id)

    @app.post("/v1/maintenance/jobs", response_model=MaintenanceReceipt)
    async def submit_maintenance(body: MaintenanceRequest, user: User):
        return await maintenance.submit(user, body)

    @app.get("/v1/maintenance/jobs", response_model=list[MaintenanceReceipt])
    async def list_maintenance(user: User):
        return await maintenance.list(user)

    @app.get("/v1/maintenance/jobs/{job_id}", response_model=MaintenanceReceipt)
    async def maintenance_status(job_id: UUID, user: User):
        return await maintenance.status(user, job_id)

    @app.post("/v1/maintenance/jobs/{job_id}/wait", response_model=MaintenanceReceipt)
    async def wait_maintenance(
        job_id: UUID,
        user: User,
        timeout_seconds: float = Query(default=30, ge=0, le=60, allow_inf_nan=False),
    ):
        return await maintenance.wait(user, job_id, wait_seconds=timeout_seconds)

    @app.post("/v1/maintenance/jobs/{job_id}/cancel", response_model=MaintenanceReceipt)
    async def cancel_maintenance(job_id: UUID, user: User):
        return await maintenance.cancel(user, job_id)

    @app.get("/v1/maintenance/artifacts/{artifact_id}", response_model=ArtifactReceipt)
    def maintenance_artifact(artifact_id: UUID, user: User):
        return context_archive.artifact(user, artifact_id)

    def is_ready():
        if not app.state.started or any(task.done() for task in app.state.workers):
            return False
        try:
            with store._control() as db:
                return db.execute(select(1)).scalar_one() == 1
        except (MemoryError, SQLAlchemyError, OSError):
            return False

    @app.get("/metrics", include_in_schema=False)
    def prometheus(user: User):
        with store._control() as db:
            store._admin(db, user)
        ready = is_ready()
        body = metrics.render(store, ready=ready, workers=len(app.state.workers))
        return Response(body, media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/v1/authority/backups")
    def authority_backups(user: User):
        return backups.list(user)

    @app.post("/v1/authority/backups", status_code=201)
    def create_authority_backup(body: BackupRequest, user: User):
        return backups.create(user, body)

    @app.get("/v1/authority/backups/{artifact_id}")
    def download_authority_backup(artifact_id: UUID, user: User):
        source, size = backups.open(user, artifact_id)

        def chunks():
            with source:
                while chunk := source.read(65536):
                    yield chunk

        return StreamingResponse(
            chunks(),
            media_type="application/octet-stream",
            headers={
                "Content-Length": str(size),
                "Content-Disposition": f'attachment; filename="{artifact_id}.mwauthority"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/v1/worlds/{world_id}/scopes/{scope_id}/lifecycle/preview")
    def preview_lifecycle(world_id: str, scope_id: UUID, body: LifecycleRequest, user: User):
        return lifecycle.preview(user, world_id, scope_id, body)

    @app.post("/v1/worlds/{world_id}/scopes/{scope_id}/lifecycle")
    def apply_lifecycle(world_id: str, scope_id: UUID, body: LifecycleRequest, user: User):
        return lifecycle.apply(user, world_id, scope_id, body)

    @app.get("/v1/worlds/{world_id}/scopes/{scope_id}/lifecycle/{operation_id}")
    def lifecycle_receipt(world_id: str, scope_id: UUID, operation_id: UUID, user: User):
        return lifecycle.receipt(user, world_id, scope_id, operation_id)

    @app.post("/v1/retention/preview")
    def preview_retention(body: RetentionRequest, user: User):
        return retention.preview(user, body)

    @app.post("/v1/retention")
    def apply_retention(body: RetentionRequest, user: User):
        return retention.apply(user, body)

    @app.get("/v1/retention/{operation_id}")
    def retention_receipt(operation_id: UUID, user: User):
        return retention.receipt(user, operation_id)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/readyz")
    def ready():
        if not is_ready():
            raise Unavailable("service workers are not ready")
        return {"status": "ready"}

    @app.get("/v1/me")
    def me(user: User):
        return {
            **user.model_dump(mode="json"),
            "authority_id": store.authority_id,
            "worlds": store.list_worlds(user),
        }

    @app.get("/v1/worlds", response_model=list[World])
    def worlds(user: User):
        return store.list_worlds(user)

    @app.get("/v1/identities/{kind}", response_model=list[NamedIdentity])
    def named_identities(kind: IdentityKind, user: User):
        return identities.list(user, kind)

    @app.post("/v1/identities/{kind}", response_model=NamedIdentity, status_code=201)
    def register_identity(kind: IdentityKind, body: CreateIdentity, user: User):
        return identities.create(user, kind, body)

    @app.get("/v1/identities/{kind}/resolve", response_model=NamedIdentity)
    def resolve_named_identity(kind: IdentityKind, reference: str, user: User):
        return identities.resolve(user, kind, reference)

    @app.get("/v1/identities/{kind}/{identity_id}", response_model=NamedIdentity)
    def named_identity(kind: IdentityKind, identity_id: UUID, user: User):
        return identities.get(user, kind, identity_id)

    @app.patch("/v1/identities/{kind}/{identity_id}", response_model=NamedIdentity)
    def rename_identity(kind: IdentityKind, identity_id: UUID, body: UpdateIdentity, user: User):
        return identities.update(user, kind, identity_id, body)

    @app.post("/v1/worlds", response_model=World, status_code=201)
    def create_world(body: CreateWorld, user: User):
        return store.create_world(user, body)

    @app.put("/v1/worlds/{world_id}/alias", response_model=World)
    def world_alias(world_id: str, body: UpdateAlias, user: User):
        return store.set_world_alias(user, world_id, body)

    @app.post("/v1/credentials", status_code=201)
    def credentials(body: CreateToken, user: User):
        token = store.issue_token(user, body)
        return {"token": token, "credential_id": store.authenticate(token).credential_id}

    @app.delete("/v1/credentials/{credential_id}", status_code=204)
    def revoke(credential_id: str, user: User):
        store.revoke_token(user, credential_id)

    @app.post("/v1/credentials/delegate", status_code=201)
    def delegate(body: DelegateCredential, user: User):
        return store.delegate_credential(user, body)

    @app.post("/v1/contexts", response_model=AgentContext, status_code=201)
    def bind_context(body: BindContext, user: User):
        return store.bind_context(user, body)

    @app.get("/v1/contexts/{context_id}", response_model=AgentContext)
    def context(context_id: UUID, user: User):
        return store.get_context(user, context_id)

    @app.get("/v1/worlds/{world_id}/scopes", response_model=list[Scope])
    def scopes(world_id: str, user: User):
        return store.list_scopes(user, world_id)

    @app.post("/v1/scopes/flow")
    def scope_flow(body: ScopeFlow, user: User):
        return {"allowed": store.scope_flow(user, body)}

    @app.post("/v1/worlds/{world_id}/scopes", response_model=Scope, status_code=201)
    def create_scope(world_id: str, body: CreateScope, user: User):
        return store.create_scope(user, world_id, body)

    @app.get("/v1/worlds/{world_id}/scopes/{scope_id}", response_model=Scope)
    def scope(world_id: str, scope_id: UUID, user: User):
        return store.get_scope(user, world_id, scope_id)

    @app.put("/v1/worlds/{world_id}/scopes/{scope_id}/alias", response_model=Scope)
    def scope_alias(world_id: str, scope_id: UUID, body: UpdateAlias, user: User):
        return store.set_scope_alias(user, world_id, scope_id, body)

    @app.put("/v1/worlds/{world_id}/scopes/{scope_id}/grants", status_code=204)
    def scope_grant(world_id: str, scope_id: UUID, body: UpdateScopeGrant, user: User):
        store.set_scope_grant(user, world_id, scope_id, body)

    @app.post("/v1/worlds/{world_id}/scopes/{scope_id}/episodes", response_model=Receipt)
    def scoped_capture(world_id: str, scope_id: UUID, body: EpisodeInput, user: User):
        if body.scope_id not in {None, scope_id}:
            from .errors import Forbidden

            raise Forbidden("Episode scope does not match its route")
        return store.capture(user, world_id, body.model_copy(update={"scope_id": scope_id}))

    @app.post("/v1/worlds/{world_id}/scopes/{scope_id}/imports", response_model=dict[str, int])
    def scoped_import(world_id: str, scope_id: UUID, body: ImportBatch, user: User):
        return store.import_episodes(user, world_id, scope_id, body.episodes)

    @app.get("/v1/worlds/{world_id}/scopes/{scope_id}/episodes", response_model=list[Episode])
    def scoped_episodes(world_id: str, scope_id: UUID, user: User, limit: int = 100):
        return store.list_episodes(user, world_id, scope_id=scope_id, limit=limit)

    @app.get(
        "/v1/worlds/{world_id}/scopes/{scope_id}/episodes/{episode_id}", response_model=Episode
    )
    def scoped_episode(world_id: str, scope_id: UUID, episode_id: str, user: User):
        return store.get_episode(user, world_id, episode_id, scope_id=scope_id)

    @app.post("/v1/worlds/{world_id}/scopes/{scope_id}/publications", response_model=Receipt)
    def scoped_publication(world_id: str, scope_id: UUID, body: ScopedPublication, user: User):
        return store.publish_scope(user, world_id, scope_id, body)

    @app.get("/v1/worlds/{world_id}/scopes/{scope_id}/snapshot")
    def scoped_snapshot(world_id: str, scope_id: UUID, user: User):
        return store.snapshot(user, world_id, scope_id=scope_id)

    @app.get("/v1/worlds/{world_id}/scopes/{scope_id}/status")
    def scoped_status(world_id: str, scope_id: UUID, user: User):
        return status(world_id, user, scope_id)

    @app.get("/v1/users")
    def users(user: User):
        return store.list_users(user)

    @app.patch("/v1/users/{user_id}", status_code=204)
    def update_user(user_id: str, body: UserUpdate, user: User):
        store.set_user_enabled(user, user_id, body.enabled)

    @app.get("/v1/worlds/{world_id}/memberships")
    def memberships(world_id: str, user: User):
        return store.list_memberships(user, world_id)

    @app.put("/v1/worlds/{world_id}/memberships/{user_id}", status_code=204)
    def update_membership(world_id: str, user_id: str, body: MembershipUpdate, user: User):
        store.set_membership(user, world_id, user_id, body.role)

    @app.post("/v1/worlds/{world_id}/episodes", response_model=Receipt)
    def capture(world_id: str, body: EpisodeInput, user: User):
        return store.capture(user, world_id, body)

    @app.get("/v1/worlds/{world_id}/episodes/{episode_id}", response_model=Episode)
    def episode(world_id: str, episode_id: str, user: User, scope_id: UUID | None = None):
        return store.get_episode(user, world_id, episode_id, scope_id=scope_id)

    @app.get("/v1/worlds/{world_id}/episodes", response_model=list[Episode])
    def episodes(world_id: str, user: User, scope_id: UUID | None = None, limit: int = 100):
        return store.list_episodes(user, world_id, scope_id=scope_id, limit=limit)

    @app.post("/v1/worlds/{world_id}/publications", response_model=Receipt)
    def publish(world_id: str, body: Publication, user: User):
        # Intentionally narrow input: publication never dereferences private evidence.
        data = EpisodeInput(**body.model_dump(), source_type="publication")
        return store.capture(user, world_id, data, shared=True)

    @app.get("/v1/worlds/{world_id}/status")
    def status(world_id: str, user: User, scope_id: UUID | None = None):
        world = store.get_world(user, world_id, scope_id=scope_id)
        episodes = store.list_episodes(user, world_id, scope_id=scope_id)
        counts: dict[str, int] = {}
        for item in episodes:
            counts[item.projection_state] = counts.get(item.projection_state, 0) + 1
        result = {"world": world, "episodes": len(episodes), "projection": counts}
        if scope_id is not None:
            result["scope"] = store.get_scope(user, world_id, scope_id)
        return result

    @app.post("/v1/search")
    async def search(body: SearchRequest, user: User):
        # Authorize the entire selection before any backend is queried.
        selections = [(wid, body.scope_id) for wid in body.world_ids]
        selections.extend((s.world_id, s.scope_id) for s in body.scopes)
        selected = [
            store.get_world(user, wid, scope_id=sid) for wid, sid in dict.fromkeys(selections)
        ]
        if body.mode == "source":
            rows = await asyncio.to_thread(
                store.search,
                user,
                body.world_ids,
                body.query,
                body.limit,
                scope_id=body.scope_id,
                scopes=body.scopes,
            )
            return {"mode": "source", "results": rows, "worlds": selected}
        if graph is None:
            raise Unavailable("graph processing is not configured; source search is available")
        kwargs = {"include_entities": True} if body.include_entities else {}
        results = await graph.search(selected, body.query, body.limit, **kwargs)
        return {"mode": "graph", "results": results, "worlds": selected}

    @app.get("/v1/mounts", response_model=list[MountView])
    def mounts(user: User):
        return router.list_mounts(user)

    @app.post("/v1/router/search")
    async def routed_search(body: RouterSearch, user: User):
        return await router.search(user, body)

    @app.post("/v1/router/publications", response_model=Delivery, status_code=202)
    def routed_publication(body: RoutedPublication, user: User):
        return router.publish(user, body)

    @app.get("/v1/router/deliveries", response_model=list[Delivery])
    def deliveries(user: User):
        return router.deliveries(user)

    @app.post("/v1/router/deliveries/{delivery_id}/retry", response_model=Delivery)
    def retry_delivery(delivery_id: str, user: User):
        return router.retry(user, delivery_id)

    return app
