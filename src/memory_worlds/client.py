"""Typed HTTP client. All agents use the daemon rather than opening its files."""

from typing import Any

import httpx

from .models import Episode, EpisodeInput, Receipt, SearchRequest, World
from .mounts import MountView
from .outbox import Delivery, RoutedPublication, RouterSearch


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
        return response.json()

    def worlds(self) -> list[World]:
        return [World.model_validate(w) for w in self.request("GET", "/v1/worlds")]

    def me(self) -> dict:
        return self.request("GET", "/v1/me")

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

    def capture(self, world_id: str, episode: EpisodeInput) -> Receipt:
        return Receipt.model_validate(
            self.request(
                "POST", f"/v1/worlds/{world_id}/episodes", json=episode.model_dump(mode="json")
            )
        )

    def get_episode(self, world_id: str, episode_id: str) -> Episode:
        return Episode.model_validate(
            self.request("GET", f"/v1/worlds/{world_id}/episodes/{episode_id}")
        )

    def search(self, request: SearchRequest) -> dict:
        return self.request("POST", "/v1/search", json=request.model_dump(mode="json"))

    def status(self, world_id: str) -> dict:
        return self.request("GET", f"/v1/worlds/{world_id}/status")
