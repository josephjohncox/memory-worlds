"""Typed HTTP client. All agents use the daemon rather than opening its files."""

from datetime import datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .errors import Conflict, NotFound
from .models import (
    AgentContext,
    BindContext,
    CreateScope,
    DelegateCredential,
    Episode,
    EpisodeInput,
    ImportBatch,
    Publication,
    Receipt,
    Scope,
    ScopedPublication,
    ScopeFlow,
    SearchRequest,
    UpdateAlias,
    UpdateScopeGrant,
    World,
)
from .mounts import MountView
from .outbox import Delivery, RoutedPublication, RouterSearch


class DelegationResult(BaseModel):
    """One-time delegated secret. Applications must store it in a private channel."""

    model_config = ConfigDict(extra="forbid")
    token: str = Field(repr=False)
    credential_id: str
    context: AgentContext
    private_scope: Scope | None = None
    expires_at: datetime | None = None


def scope_path(world_id: str, scope_id: UUID | str | None = None) -> str:
    path = f"/v1/worlds/{quote(world_id, safe='')}"
    return path if scope_id is None else f"{path}/scopes/{UUID(str(scope_id))}"


class Client:
    def __init__(self, url: str, token: str, *, timeout: float = 30):
        self.http = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.http.close()

    def request(self, method: str, path: str, **kwargs) -> Any:
        response = self.http.request(method, path, **kwargs)
        response.raise_for_status()
        return None if response.status_code == 204 else response.json()

    def worlds(self) -> list[World]:
        return [World.model_validate(w) for w in self.request("GET", "/v1/worlds")]

    def import_episodes(
        self, world_id: str, scope_id: UUID | str, episodes: list[EpisodeInput]
    ) -> dict:
        """Atomically import historical evidence into an explicit private scope."""
        batch = ImportBatch(episodes=episodes)
        return self.request(
            "POST",
            f"{scope_path(world_id, scope_id)}/imports",
            json=batch.model_dump(mode="json"),
        )

    @staticmethod
    def _alias(items, reference: str, kind: str):
        matches = [item for item in items if item.alias == reference[1:]]
        if not matches:
            raise NotFound(f"No accessible {kind} matches that alias")
        if len(matches) != 1:
            raise Conflict(f"Ambiguous {kind} alias; select a canonical ID from discovery")
        return matches[0]

    def resolve_world(self, reference: str) -> str:
        """Resolve only explicit @aliases, among this credential's visible worlds."""
        return (
            str(self._alias(self.worlds(), reference, "world").id)
            if reference.startswith("@")
            else reference
        )

    def resolve_scope(self, world_id: str, reference: UUID | str) -> UUID:
        value = str(reference)
        if value.startswith("@"):
            return self._alias(self.scopes(world_id), value, "scope").id
        return UUID(value)

    def set_world_alias(self, world_id: str, request: UpdateAlias) -> World:
        return World.model_validate(
            self.request(
                "PUT", f"{scope_path(world_id)}/alias", json=request.model_dump(mode="json")
            )
        )

    def set_scope_alias(self, world_id: str, scope_id: UUID, request: UpdateAlias) -> Scope:
        return Scope.model_validate(
            self.request(
                "PUT",
                f"{scope_path(world_id, scope_id)}/alias",
                json=request.model_dump(mode="json"),
            )
        )

    def discover(self, *, world: str | None = None, include_scopes: bool = False) -> dict:
        worlds = self.worlds()
        if world is not None:
            selected = self.resolve_world(world)
            worlds = [item for item in worlds if item.id == selected]
            if not worlds:
                raise NotFound("World not found")
        return {
            "worlds": worlds,
            "scopes": {item.id: self.scopes(item.id) for item in worlds}
            if include_scopes or world
            else {},
            "mounts": self.mounts(),
        }

    def me(self) -> dict:
        return self.request("GET", "/v1/me")

    def scopes(self, world_id: str) -> list[Scope]:
        return [
            Scope.model_validate(value)
            for value in self.request("GET", f"{scope_path(world_id)}/scopes")
        ]

    def get_scope(self, world_id: str, scope_id: UUID | str) -> Scope:
        return Scope.model_validate(self.request("GET", scope_path(world_id, scope_id)))

    def create_scope(self, world_id: str, request: CreateScope) -> Scope:
        return Scope.model_validate(
            self.request(
                "POST", f"{scope_path(world_id)}/scopes", json=request.model_dump(mode="json")
            )
        )

    def grant_scope(self, world_id: str, scope_id: UUID | str, request: UpdateScopeGrant) -> None:
        self.request(
            "PUT", f"{scope_path(world_id, scope_id)}/grants", json=request.model_dump(mode="json")
        )

    def bind_context(self, request: BindContext) -> AgentContext:
        return AgentContext.model_validate(
            self.request("POST", "/v1/contexts", json=request.model_dump(mode="json"))
        )

    def get_context(self, context_id: UUID | str) -> AgentContext:
        return AgentContext.model_validate(
            self.request("GET", f"/v1/contexts/{UUID(str(context_id))}")
        )

    def can_flow(self, request: ScopeFlow) -> bool:
        """Check current audience containment without disclosing reader identities."""
        response = self.request("POST", "/v1/scopes/flow", json=request.model_dump(mode="json"))
        if not isinstance(response, dict) or not isinstance(response.get("allowed"), bool):
            raise ValueError("invalid scope flow response")
        return response["allowed"]

    def delegate(self, request: DelegateCredential) -> DelegationResult:
        return DelegationResult.model_validate(
            self.request("POST", "/v1/credentials/delegate", json=request.model_dump(mode="json"))
        )

    def mounts(self) -> list[MountView]:
        return [MountView.model_validate(m) for m in self.request("GET", "/v1/mounts")]

    def routed_search(self, request: RouterSearch) -> dict:
        return self.request("POST", "/v1/router/search", json=request.model_dump(mode="json"))

    def publish_remote(self, request: RoutedPublication) -> Delivery:
        return Delivery.model_validate(
            self.request("POST", "/v1/router/publications", json=request.model_dump(mode="json"))
        )

    def deliveries(self) -> list[Delivery]:
        return [Delivery.model_validate(d) for d in self.request("GET", "/v1/router/deliveries")]

    def retry_delivery(self, delivery_id: str) -> Delivery:
        return Delivery.model_validate(
            self.request("POST", f"/v1/router/deliveries/{delivery_id}/retry")
        )

    def capture(
        self, world_id: str, episode: EpisodeInput, *, scope_id: UUID | str | None = None
    ) -> Receipt:
        if scope_id is not None:
            selected = UUID(str(scope_id))
            if episode.scope_id is not None and episode.scope_id != selected:
                raise ValueError("episode scope does not match the selected scope")
            episode = episode.model_copy(update={"scope_id": selected})
        return Receipt.model_validate(
            self.request(
                "POST", f"{scope_path(world_id)}/episodes", json=episode.model_dump(mode="json")
            )
        )

    def get_episode(
        self, world_id: str, episode_id: str, *, scope_id: UUID | str | None = None
    ) -> Episode:
        return Episode.model_validate(
            self.request(
                "GET", f"{scope_path(world_id, scope_id)}/episodes/{quote(episode_id, safe='')}"
            )
        )

    def episodes(
        self, world_id: str, *, scope_id: UUID | str | None = None, limit: int = 20
    ) -> list[Episode]:
        if not 1 <= limit <= 1000:
            raise ValueError("episode limit must be between 1 and 1000")
        return [
            Episode.model_validate(value)
            for value in self.request(
                "GET", f"{scope_path(world_id, scope_id)}/episodes", params={"limit": limit}
            )
        ]

    def publish(
        self,
        world_id: str,
        request: Publication | ScopedPublication,
        *,
        scope_id: UUID | str | None = None,
    ) -> Receipt:
        if scope_id is None and isinstance(request, ScopedPublication):
            raise ValueError("scoped publication requires an explicit destination scope")
        return Receipt.model_validate(
            self.request(
                "POST",
                f"{scope_path(world_id, scope_id)}/publications",
                json=request.model_dump(mode="json"),
            )
        )

    def search(self, request: SearchRequest) -> dict:
        return self.request("POST", "/v1/search", json=request.model_dump(mode="json"))

    def status(self, world_id: str, *, scope_id: UUID | str | None = None) -> dict:
        return self.request("GET", f"{scope_path(world_id, scope_id)}/status")
