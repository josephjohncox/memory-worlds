import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import __version__
from .auth import AuthConfig, OIDCAuthenticator
from .errors import MemoryError, Unauthorized, Unavailable
from .locking import DaemonLock
from .models import (
    AgentContext,
    BindContext,
    CreateScope,
    CreateToken,
    CreateWorld,
    DelegateCredential,
    Episode,
    EpisodeInput,
    MembershipUpdate,
    Principal,
    Publication,
    Receipt,
    Scope,
    ScopedPublication,
    ScopeFlow,
    SearchRequest,
    UpdateScopeGrant,
    UserUpdate,
    World,
)
from .mounts import MountConfig, MountView
from .outbox import Delivery, RoutedPublication, RouterSearch
from .router import WorldRouter
from .store import MemoryStore

bearer = HTTPBearer(auto_error=False)


def create_app(data_dir: Path, graph=None, *, router=None, auth=None, mount_config=None) -> FastAPI:
    store = MemoryStore(data_dir)
    if auth is None:
        config = AuthConfig.from_env()
        auth = OIDCAuthenticator(store, config) if config else None
    router = router or WorldRouter(store, mount_config or MountConfig.from_env())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with DaemonLock(data_dir):
            store.reconcile_oidc_issuer(auth.config.issuer if auth is not None else None)
            if auth is not None:
                auth.reconcile()
            tasks = []
            if graph is not None:
                tasks.append(asyncio.create_task(graph.run(), name="graph-projection"))
            if router.mounts:
                tasks.append(asyncio.create_task(router.run(), name="publication-outbox"))
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

    app = FastAPI(title="Memory Worlds", version=__version__, lifespan=lifespan)
    app.state.store = store
    app.state.router = router
    app.state.started = False
    app.state.workers = []

    @app.middleware("http")
    async def bounded_body(request: Request, call_next):
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 1_048_576:
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

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/readyz")
    def ready():
        if not app.state.started or any(task.done() for task in app.state.workers):
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

    @app.post("/v1/worlds", response_model=World, status_code=201)
    def create_world(body: CreateWorld, user: User):
        return store.create_world(user, body)

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

    @app.put("/v1/worlds/{world_id}/scopes/{scope_id}/grants", status_code=204)
    def scope_grant(world_id: str, scope_id: UUID, body: UpdateScopeGrant, user: User):
        store.set_scope_grant(user, world_id, scope_id, body)

    @app.post("/v1/worlds/{world_id}/scopes/{scope_id}/episodes", response_model=Receipt)
    def scoped_capture(world_id: str, scope_id: UUID, body: EpisodeInput, user: User):
        if body.scope_id not in {None, scope_id}:
            from .errors import Forbidden

            raise Forbidden("Episode scope does not match its route")
        return store.capture(user, world_id, body.model_copy(update={"scope_id": scope_id}))

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
