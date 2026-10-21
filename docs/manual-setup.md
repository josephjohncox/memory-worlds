# Configure a local daemon manually

[Documentation](README.md)

Use this path when you want to manage the daemon, credentials and graph
configuration yourself. The [guided setup](setup.md) automates these steps and
creates separate client profiles. Choose one setup path for an installation.

## Initialize the authority

Linux and macOS are the initial targets. The daemon uses Unix file locking.
Install Docker with Compose support and start its local engine before continuing.
Use [mise](https://mise.jdx.dev/) to install the pinned toolchain and run tasks.
The checked-in mise, uv, and npm lockfiles record the tool and library versions.
See [development tasks](development.md). Configure explicit extraction and
embedding models in a private `.env` before starting the service; see
[Graphiti configuration](graphiti.md).

```sh
mise trust
mise install --locked
mise run deps
cp .env.example .env
chmod 600 .env
# Edit .env: choose models/endpoints and a random 64-hex graph password.
mise run graph:backend-up
umask 077
mkdir -p ~/.config/memory-worlds
mise run cli -- init --user alice > ~/.config/memory-worlds/bootstrap.json
mise exec -- python - <<'PY'
import json
from pathlib import Path
folder = Path.home() / ".config/memory-worlds"
bootstrap = json.loads((folder / "bootstrap.json").read_text())
(folder / "admin.token").write_text(bootstrap["token"] + "\n")
print("Personal world:", bootstrap["world"]["id"])
PY
mise run cli -- serve
```

Keep the bootstrap output private: it contains the initial administrative
credential. For this manual configuration, runtime data defaults to
`~/.local/share/memory-worlds`, outside the repository. `--data-dir PATH` selects
another local directory. `init` refuses to overwrite an initialized authority.
The daemon binds `127.0.0.1:8787`; a second
daemon cannot take the same directory. Starting the daemon alone does not create
an owner credential.

## Give an agent access

In a second terminal, replace `WORLD_ID` with the printed ID:

```sh
mise run cli -- --token-file ~/.config/memory-worlds/admin.token worlds
mise run cli -- --token-file ~/.config/memory-worlds/admin.token \
  token-create --user alice --agent editor --grant WORLD_ID:writer
```

Save the returned token in a private file for that agent. The response includes
its credential ID for `token-revoke`. Agent roles are `reader`, `writer`, and
`manager`; all can read, while writer/manager can contribute. Administration is
a separate credential capability. Use routine agent credentials for agents.

```sh
mise run cli -- --token-file ~/.config/memory-worlds/editor.token \
  capture WORLD_ID --source-id session-42-entry-7 \
  --at 2026-10-20T12:00:00Z --text 'The fixture spindle uses a ceramic bearing.'
mise run cli -- --token-file ~/.config/memory-worlds/editor.token \
  search 'ceramic bearing' --world WORLD_ID
```

Repeat the same source ID, timestamp, content, and metadata on retries. The
service returns the original receipt. Reusing an ID with changed input returns
HTTP 409. Capture queues Graphiti extraction by default; recall defaults to graph
retrieval. `search --source` explicitly inspects the durable source journal, while
`capture --source-only` deliberately skips extraction for that record. A failed
graph query reports its error and never silently changes retrieval modes.

The HTTP contract is at `/docs` and `/openapi.json`, with an inspectable
[OpenAPI artifact](openapi.json) in the repository. The [Python client
example](agents.md) and MCP adapter use that same service. `/healthz` checks
the HTTP process; `/readyz` also checks that required background workers are running.

See [scoped agent clients](agents.md) for private read/write selections and
[SSH access](remote-access.md) for agents on other machines.
