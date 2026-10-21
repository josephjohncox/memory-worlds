# Connect remote agents over SSH

[Documentation](README.md)

Agents connect to the authenticated HTTP service through a loopback tunnel.
Give each agent its own credential for the selected worlds and scopes.

## Agent connects to a memory host

From a machine running an agent, forward a local port to the memory host:

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:8788:127.0.0.1:8787 user@memory-host
```

Configure the remote agent with `MEMORY_WORLDS_URL=http://127.0.0.1:8788` and its
own token, or use CLI `--url`. Application authentication remains required
through the tunnel. Raw graph ports are never needed by agent clients.

## Remote compute uses your laptop daemon

If the daemon runs on your laptop and the agent runs on a remote compute node,
run this on the laptop instead:

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -R 127.0.0.1:8788:127.0.0.1:8787 user@compute-node
```

The compute-node agent then uses `http://127.0.0.1:8788`. Forwarding requires the
SSH host's forwarding policy to permit it. For a public/network endpoint,
configure authenticated TLS ingress and explicitly enable non-loopback binding;
the local setup does not require opening a public listener.

Use [discovery](discovery.md) to check the credential's visible worlds and scopes.
For local memory alongside a corporate service, see [hybrid setup](hybrid.md).
