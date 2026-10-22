"""Real HTTP integration of setup discovery and credential-scoped corporate mounts."""

import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx
import pytest
import uvicorn

from memory_worlds.api import create_app
from memory_worlds.client import Client
from memory_worlds.errors import MemoryError
from memory_worlds.models import CreateToken, CreateWorld, EpisodeInput, Role, WorldKind
from memory_worlds.mounts import MountConfig
from memory_worlds.setup_config import SetupConfig
from memory_worlds.setup_files import private_read, read_json, write_private
from memory_worlds.setup_interactive import connect_setup
from memory_worlds.setup_provision import apply_setup
from memory_worlds.setup_runtime import runtime_environment
from memory_worlds.store import MemoryStore


@contextmanager
def serve(store, mounts=None):
    app = create_app(store.data_dir, store=store, mount_config=mounts or MountConfig())
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.02)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


def test_normal_user_hybrid_discovery_and_mount_audiences_over_http(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("MEMORY_WORLDS_"):
            monkeypatch.delenv(key)
    corporate = MemoryStore(tmp_path / "corporate")
    _, admin_token = corporate.bootstrap("operator")
    admin = corporate.authenticate(admin_token)
    shared = corporate.create_world(admin, CreateWorld(name="Engineering", kind=WorldKind.TEAM))
    private = corporate.create_world(
        admin, CreateWorld(name="User private work", kind=WorldKind.PRIVATE_WORK, owner_id="alice")
    )
    user_token = corporate.issue_token(
        admin,
        CreateToken(
            user_id="alice",
            agent_id="corporate-client",
            grants={shared.id: Role.READER, private.id: Role.WRITER},
        ),
    )
    corporate.capture(
        admin,
        shared.id,
        EpisodeInput(
            source_id="shared-procedure",
            content="The team procedure uses a cobalt queue.",
            occurred_at=datetime.now(UTC),
            project=False,
        ),
        shared=True,
    )
    provider = {
        "provider": "openai-compatible",
        "model": "explicit",
        "base_url": "http://127.0.0.1:1/v1",
    }
    external = tmp_path / "credentials" / "user.token"
    write_private(external, user_token + "\n")
    with serve(corporate) as url:
        config = SetupConfig(
            instance="hybrid-http",
            owner="alice",
            mode="hybrid",
            data_dir=tmp_path / "local",
            state_dir=tmp_path / "state",
            extraction=provider,
            embedding=provider,
            corporate={"url": url, "token_file": external, "world_ids": [shared.id]},
        )
        bound = connect_setup(config)
        assert bound["authority_id"] == corporate.authority_id
        assert bound["user_id"] == "alice"
        assert bound["world_ids"] == [shared.id]
        assert not corporate.authenticate(user_token).admin
        apply_setup(config)
        environment = runtime_environment(config)
        assert environment["MEMORY_WORLDS_GRAPH_BACKEND"] == "falkordb"
        assert environment["MEMORY_WORLDS_STORAGE_BACKEND"] == "sqlite"
        assert admin_token not in json.dumps(read_json(config.path("state.json")))
        local = MemoryStore(config.data_dir)
        mounts = MountConfig.model_validate(read_json(config.path("mounts.json")))
        with serve(local, mounts) as local_url:
            personal = private_read(config.path("personal.token")).decode().strip()
            work = private_read(config.path("work.token")).decode().strip()
            body = {"mount_ids": ["corporate"], "query": "cobalt", "mode": "source"}
            with Client(local_url, personal) as client:
                assert client.mounts() == []
                with pytest.raises(httpx.HTTPStatusError) as caught:
                    client.request("POST", "/v1/router/search", json=body)
                assert caught.value.response.status_code == 403
            with Client(local_url, work) as client:
                assert [mount.id for mount in client.mounts()] == ["corporate"]
                result = client.request("POST", "/v1/router/search", json=body)
                assert "cobalt queue" in json.dumps(result)
                assert private.id not in json.dumps(result)
        # Export references the applied corporate binding, never a replaced token.
        from memory_worlds.client_setup import connection
        from memory_worlds.setup_config import save_config

        config_file = tmp_path / "config" / "setup.toml"
        save_config(config, config_file)
        profile = connection(config_file, "corporate")
        assert profile["url"] == url
        assert profile["tokenFile"] == str(config.path("corporate.token"))
        assert profile["readScopes"] == [] and "writeScope" not in profile
        token_path = config.path("corporate.token")
        original_token = private_read(token_path)
        token_path.write_text("replaced-corporate-token\n")
        with pytest.raises(MemoryError):
            connection(config_file, "corporate")
        token_path.write_bytes(original_token)
        # An explicit private-world selection cannot become a router mount.
        config.corporate.world_ids = [private.id]
        with pytest.raises(MemoryError, match="accessible shared"):
            connect_setup(config)
        assert read_json(config.path("corporate.json"))["world_ids"] == [shared.id]
