"""Federated recall and explicit, durable publication across memory authorities."""

import asyncio
import hashlib
import json

import httpx
from pydantic import ValidationError

from .errors import Forbidden, MemoryError, NotFound
from .models import Episode, EpisodeInput, Principal, Receipt, World, WorldKind
from .mounts import MountConfig, MountRole, MountView
from .outbox import PublicationOutbox, RoutedPublication, RouterSearch
from .scopes import world_shared_scope_id


class RemoteFailure(Exception):
    def __init__(self, code: str, retryable: bool = False):
        self.code, self.retryable = code, retryable


class WorldRouter:
    def __init__(self, store, config: MountConfig, *, transport=None, timeout: float = 10):
        self.store = store
        self.mounts = {mount.id: mount for mount in config.validated().mounts}
        self.outbox = PublicationOutbox(store.data_dir, store.authority_id)
        self.transport, self.timeout = transport, timeout
        self._recovered = False
        self._worker_lock = asyncio.Lock()

    def mount(self, principal: Principal, mount_id: str, publish: bool = False):
        self.store.validate_principal(principal)
        mount = self.mounts.get(mount_id)
        if mount is None:
            raise NotFound("mount not found")
        mount.authorize(principal, publish)
        return mount

    def list_mounts(self, principal: Principal) -> list[MountView]:
        self.store.validate_principal(principal)
        return [
            MountView(
                id=m.id,
                name=m.name,
                url=m.url,
                authority_id=m.authority_id,
                world_ids=m.world_ids,
                can_publish=m.publish and m.role(principal) == MountRole.PUBLISHER,
            )
            for m in self.mounts.values()
            if m.role(principal) is not None
        ]

    def client(self, mount):
        return httpx.AsyncClient(
            base_url=mount.url,
            headers={"Authorization": "Bearer " + mount.token()},
            timeout=self.timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        )

    @staticmethod
    async def request(client, method, path, **kwargs):
        async with client.stream(method, path, **kwargs) as response:
            if not response.is_success:
                status = response.status_code
                raise RemoteFailure(f"remote_http_{status}", status in {408, 429} or status >= 500)
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > 4_194_304:
                    raise RemoteFailure("remote_response_too_large")
                content.extend(chunk)
        try:
            return json.loads(content)
        except (ValueError, RecursionError) as error:
            # A bounded byte count does not bound a JSON parser's nesting depth.
            # Malformed upstream data must not terminate the durable outbox worker.
            raise RemoteFailure("remote_protocol_error") from error

    async def worlds(self, client, mount, world_ids) -> dict[str, World]:
        data = await self.request(client, "GET", "/v1/worlds")
        if not isinstance(data, list):
            raise RemoteFailure("remote_protocol_error")
        parsed = [World.model_validate(item) for item in data]
        worlds = {world.id: world for world in parsed}
        if len(worlds) != len(parsed):
            raise RemoteFailure("remote_protocol_error")
        if any(wid not in worlds for wid in world_ids):
            raise RemoteFailure("remote_world_access_denied")
        if any(worlds[wid].authority_id != mount.authority_id for wid in world_ids):
            raise RemoteFailure("remote_authority_mismatch")
        if any(
            worlds[wid].kind in {WorldKind.PERSONAL, WorldKind.PRIVATE_WORK} for wid in world_ids
        ):
            raise RemoteFailure("remote_world_is_private")
        if any(
            str(worlds[wid].scope_id) != world_shared_scope_id(mount.authority_id, wid)
            for wid in world_ids
        ):
            raise RemoteFailure("remote_scope_mismatch")
        return {wid: worlds[wid] for wid in world_ids}

    async def search(self, principal: Principal, request: RouterSearch):
        # Resolve/authorize every mount before sending any remote request.
        selected = [self.mount(principal, mid) for mid in dict.fromkeys(request.mount_ids)]
        semaphore = asyncio.Semaphore(4)

        async def query(mount):
            try:
                async with semaphore, self.client(mount) as client:
                    await self.worlds(client, mount, mount.world_ids)
                    self.mount(principal, mount.id)
                    body = await self.request(
                        client,
                        "POST",
                        "/v1/search",
                        json={
                            "world_ids": mount.world_ids,
                            "query": request.query,
                            "mode": request.mode,
                            "limit": request.limit,
                        },
                    )
                self.mount(principal, mount.id)
                if (
                    not isinstance(body, dict)
                    or body.get("mode") != request.mode
                    or not isinstance(body.get("results"), list)
                ):
                    raise RemoteFailure("remote_protocol_error")
                results = []
                for raw in body["results"][: request.limit]:
                    if request.mode == "source":
                        episode = Episode.model_validate(raw)
                        row = episode.model_dump(mode="json")
                        row["record_type"] = "source"
                    else:
                        if not isinstance(raw, dict) or not isinstance(raw.get("fact"), str):
                            raise RemoteFailure("remote_protocol_error")
                        row = dict(raw)
                        if row.get("authority_id") != mount.authority_id:
                            raise RemoteFailure("remote_authority_mismatch")
                    if row.get("world_id") not in mount.world_ids:
                        raise RemoteFailure("remote_scope_mismatch")
                    if row.get("scope_id") != world_shared_scope_id(
                        mount.authority_id, row["world_id"]
                    ):
                        raise RemoteFailure("remote_scope_mismatch")
                    row.update(mount_id=mount.id, authority_id=mount.authority_id)
                    results.append(row)
                return results, None
            except RemoteFailure as error:
                return [], {"mount_id": mount.id, "error": error.code}
            except (httpx.HTTPError, OSError):
                return [], {"mount_id": mount.id, "error": "remote_unavailable"}
            except (ValueError, ValidationError, TypeError, KeyError, MemoryError):
                return [], {"mount_id": mount.id, "error": "remote_protocol_error"}

        replies = await asyncio.gather(*(query(mount) for mount in selected))
        results = [result for rows, _ in replies for result in rows]
        failures = [error for _, error in replies if error is not None]
        return {"results": results[: request.limit], "failures": failures, "complete": not failures}

    def publish(self, principal: Principal, request: RoutedPublication):
        mount = self.mount(principal, request.destination_mount_id, publish=True)
        if request.world_id not in mount.world_ids:
            raise Forbidden("publication destination is outside the mounted worlds")
        return self.outbox.enqueue(principal, mount, request)

    async def _deliver(self, row):
        delivery_id = row["id"]
        try:
            principal = Principal.model_validate_json(row["principal"])
            mount = self.mount(principal, row["mount_id"], publish=True)
            if (
                mount.url != row["endpoint"]
                or mount.authority_id != row["authority_id"]
                or row["world_id"] not in mount.world_ids
            ):
                raise RemoteFailure("destination_changed")
            async with self.client(mount) as client:
                await self.worlds(client, mount, [row["world_id"]])
                # Metadata IO may take time. Revocation before dispatch stops this POST.
                self.mount(principal, mount.id, publish=True)
                payload = json.loads(row["payload"])
                data = await self.request(
                    client, "POST", f"/v1/worlds/{row['world_id']}/publications", json=payload
                )
                receipt = Receipt.model_validate(data)
                expected = EpisodeInput(
                    **payload,
                    source_type="publication",
                    scope_id=world_shared_scope_id(mount.authority_id, row["world_id"]),
                )
                actual = EpisodeInput.model_validate(
                    receipt.episode.model_dump(include=set(EpisodeInput.model_fields))
                )
                canonical_payload = expected.model_dump(mode="json")
                canonical = json.dumps(
                    canonical_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                if (
                    receipt.episode.world_id != row["world_id"]
                    or actual != expected
                    or receipt.episode.digest != hashlib.sha256(canonical.encode()).hexdigest()
                ):
                    raise RemoteFailure("remote_receipt_mismatch")
            self.outbox.finish(delivery_id, "delivered", episode_id=receipt.episode.id)
        except RemoteFailure as error:
            self.outbox.finish(delivery_id, "pending" if error.retryable else "blocked", error.code)
        except (httpx.HTTPError, OSError):
            self.outbox.finish(delivery_id, "pending", "remote_unavailable")
        except (ValueError, ValidationError, TypeError, KeyError):
            self.outbox.finish(delivery_id, "blocked", "remote_protocol_error")
        except MemoryError:
            self.outbox.finish(
                delivery_id, "blocked", "local_authorization_or_configuration_changed"
            )

    async def run_once(self):
        async with self._worker_lock:
            if not self._recovered:
                self.outbox.recover()
                self._recovered = True
            jobs = []
            for _ in range(20):
                row = self.outbox.claim()
                if row is None:
                    break
                jobs.append(row)
            semaphore = asyncio.Semaphore(4)

            async def deliver(row):
                async with semaphore:
                    await self._deliver(row)

            await asyncio.gather(*(deliver(row) for row in jobs))
            return len(jobs)

    async def run(self):
        while True:
            await self.run_once()
            await asyncio.sleep(1)

    def deliveries(self, principal):
        self.store.validate_principal(principal)
        if principal.restricted:
            raise Forbidden("Delegated scope credentials cannot inspect federation deliveries")
        return self.outbox.list(principal)

    def retry(self, principal, delivery_id):
        self.store.validate_principal(principal)
        if principal.restricted:
            raise Forbidden("Delegated scope credentials cannot retry federation deliveries")
        row = self.outbox.owned(principal, delivery_id)
        mount = self.mount(principal, row["mount_id"], publish=True)
        if (
            mount.url != row["endpoint"]
            or mount.authority_id != row["authority_id"]
            or row["world_id"] not in mount.world_ids
        ):
            raise Forbidden("publication destination has changed")
        return self.outbox.retry(principal, delivery_id)
