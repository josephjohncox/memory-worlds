"""Verify the documented SSH topology without changing host SSH configuration."""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from test_cli import AT, _daemon, _invoke


def _port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def _stop(process):
    if process.poll() is None:
        process.terminate()
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)
        raise AssertionError("SSH fixture process did not stop") from None


@contextmanager
def _forward(tmp_path, destination_port):
    with tempfile.TemporaryDirectory(prefix="isolated-ssh-", dir=tmp_path) as directory:
        with _forward_in_directory(Path(directory), destination_port) as url:
            yield url


@contextmanager
def _forward_in_directory(folder, destination_port):
    if os.name != "posix":
        pytest.skip("SSH fixture requires Unix and an unprivileged local account")
    import pwd

    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    ssh = shutil.which("ssh")
    keygen = shutil.which("ssh-keygen")
    if not Path(sshd).is_file() or ssh is None or keygen is None:
        pytest.skip("SSH forwarding check requires preinstalled sshd, ssh, and ssh-keygen")
    if os.getuid() == 0:
        pytest.skip("SSH fixture intentionally runs as an unprivileged account")
    username = pwd.getpwuid(os.getuid()).pw_name
    for name in ("host", "client"):
        subprocess.run(
            [keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(folder / name)],
            capture_output=True,
            check=True,
            timeout=5,
        )
    authorized = folder / "authorized_keys"
    authorized.write_text(
        f'restrict,port-forwarding,permitopen="127.0.0.1:{destination_port}" '
        + (folder / "client.pub").read_text()
    )
    authorized.chmod(0o600)
    sshd_port = _port()
    configuration = folder / "sshd_config"
    configuration.write_text(
        f"""Port {sshd_port}
ListenAddress 127.0.0.1
AddressFamily inet
HostKey {json.dumps(str(folder / "host"))}
PidFile {json.dumps(str(folder / "sshd.pid"))}
AuthorizedKeysFile {json.dumps(str(authorized))}
AllowUsers {username}
AuthenticationMethods publickey
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
PermitRootLogin no
StrictModes no
AllowTcpForwarding local
PermitOpen 127.0.0.1:{destination_port}
AllowStreamLocalForwarding no
AllowAgentForwarding no
X11Forwarding no
PermitTunnel no
GatewayPorts no
MaxSessions 0
UseDNS no
LogLevel ERROR
"""
    )
    configuration.chmod(0o600)
    server = subprocess.Popen(
        [sshd, "-D", "-e", "-f", str(configuration)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    client = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if server.poll() is not None:
                _, error = server.communicate()
                pytest.skip(f"Unprivileged standalone sshd unavailable: {error.strip()}")
            try:
                with socket.create_connection(("127.0.0.1", sshd_port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("Temporary sshd did not start on loopback")

        known_hosts = folder / "known_hosts"
        host_key = (folder / "host.pub").read_text().split()
        known_hosts.write_text(f"[127.0.0.1]:{sshd_port} {host_key[0]} {host_key[1]}\n")
        known_hosts.chmod(0o600)
        forwarded_port = _port()
        client = subprocess.Popen(
            [
                ssh,
                "-F",
                "/dev/null",
                "-N",
                "-p",
                str(sshd_port),
                "-i",
                str(folder / "client"),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "IdentityAgent=none",
                "-o",
                "BatchMode=yes",
                "-o",
                "PasswordAuthentication=no",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "GlobalKnownHostsFile=/dev/null",
                "-o",
                f"UserKnownHostsFile={known_hosts}",
                "-o",
                "ExitOnForwardFailure=yes",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "LogLevel=ERROR",
                "-L",
                f"127.0.0.1:{forwarded_port}:127.0.0.1:{destination_port}",
                f"{username}@127.0.0.1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        url = f"http://127.0.0.1:{forwarded_port}"
        deadline = time.monotonic() + 10
        with httpx.Client(base_url=url, timeout=0.3, trust_env=False) as health:
            while time.monotonic() < deadline:
                if client.poll() is not None:
                    _, error = client.communicate()
                    raise AssertionError(f"SSH forwarding failed: {error.strip()}")
                try:
                    if health.get("/healthz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                raise AssertionError("Memory API did not become reachable through SSH")
        yield url
    finally:
        if client is not None:
            _stop(client)
        _stop(server)


def test_authenticated_memory_capture_and_search_through_real_ssh(tmp_path):
    data_dir = tmp_path / "authority"
    initialized = _invoke(data_dir, "init", "--user", "alice")
    administrator, world = initialized["token"], initialized["world"]["id"]
    with _daemon(data_dir) as direct_url:
        agent = _invoke(
            data_dir,
            "token-create",
            "--user",
            "alice",
            "--agent",
            "ssh-agent",
            "--grant",
            f"{world}:writer",
            url=direct_url,
            token=administrator,
        )["token"]
        with _forward(tmp_path, urlparse(direct_url).port) as tunnel_url:
            # SSH authenticates the transport; the application still requires its own credential.
            with httpx.Client(base_url=tunnel_url, trust_env=False, timeout=3) as unauthenticated:
                assert unauthenticated.get("/v1/worlds").status_code == 401
            receipt = _invoke(
                data_dir,
                "capture",
                world,
                "--source-id",
                "ssh-fixture-1",
                "--at",
                AT,
                "--text",
                "Synthetic evidence delivered through SSH",
                url=tunnel_url,
                token=agent,
            )
            assert receipt["episode"]["agent_id"] == "ssh-agent"
            found = _invoke(
                data_dir,
                "search",
                "--source",
                "through SSH",
                "--world",
                world,
                url=tunnel_url,
                token=agent,
            )
            assert [item["id"] for item in found["results"]] == [receipt["episode"]["id"]]
        found = _invoke(
            data_dir,
            "search",
            "--source",
            "through SSH",
            "--world",
            world,
            url=direct_url,
            token=administrator,
        )
        assert found["results"][0]["source_id"] == "ssh-fixture-1"
    assert not list(tmp_path.glob("isolated-ssh-*"))
