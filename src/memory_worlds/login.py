"""Native OAuth device login; retain only an API-validated access token."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .auth import _SCOPE, _trusted_url
from .errors import MemoryError

_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def _origin(value: str) -> tuple:
    parsed = urlsplit(value)
    return (
        parsed.scheme.lower(),
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


def _endpoint(value, issuer: str) -> str:
    if not isinstance(value, str) or len(value) > 4096 or any(c.isspace() for c in value):
        raise MemoryError("Identity provider returned an invalid endpoint")
    _trusted_url(value)
    if _origin(value) != _origin(issuer):
        raise MemoryError("Identity provider endpoints must use the configured issuer origin")
    return value


def _document(client: httpx.Client, method: str, url: str, **kwargs) -> tuple[int, dict]:
    try:
        with client.stream(method, url, **kwargs) as response:
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 1_048_576:
                    raise MemoryError("Identity response exceeds one MiB")
            if response.status_code not in {200, 400}:
                raise MemoryError(f"Login endpoint returned HTTP {response.status_code}")
        document = json.loads(content)
        if not isinstance(document, dict):
            raise ValueError("object required")
        return response.status_code, document
    except (ValueError, RecursionError) as error:
        raise MemoryError("Login endpoint returned invalid JSON") from error


def _number(value, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise MemoryError(f"Identity provider returned invalid {name}")
    return value


def _secret(value, name: str, maximum: int = 16_384) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(c.isspace() or not c.isascii() or not c.isprintable() for c in value)
    ):
        raise MemoryError(f"Identity provider returned invalid {name}")
    return value


def _wait(interval: float, deadline: float) -> None:
    stop = min(time.monotonic() + interval, deadline)
    while (remaining := stop - time.monotonic()) > 0:
        time.sleep(min(remaining, 30))
    if time.monotonic() >= deadline:
        raise MemoryError("Device authorization expired; start login again")


def _save_token(destination: Path, token: str) -> None:
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(token + "\n")
            output.flush()
            os.fsync(output.fileno())
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def device_login(
    url: str,
    *,
    issuer: str,
    client_id: str,
    scopes: tuple[str, ...] = ("openid", "memory-worlds"),
    token_output: Path,
) -> dict:
    """Sign in without a client secret or callback listener; never print tokens.

    The caller supplies the trusted service and issuer. Discovery cannot redirect
    credentials to another origin. The service independently verifies the access
    token before the exclusive private file is created. No refresh token is kept.
    """
    _trusted_url(issuer)
    _trusted_url(url)
    _secret(client_id, "client ID", 2000)
    if (
        not scopes
        or len(scopes) > 100
        or any(not isinstance(scope, str) or not _SCOPE.fullmatch(scope) for scope in scopes)
    ):
        raise MemoryError("Login requires bounded OAuth scope names")
    if "offline_access" in scopes:
        raise MemoryError("Device login does not retain refresh tokens; omit offline_access")
    if os.path.lexists(token_output):
        raise MemoryError("Token output already exists; choose a new private file")
    if not token_output.parent.is_dir():
        raise MemoryError("Create the private token output directory before login")
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        status, metadata = _document(
            client, "GET", issuer.rstrip("/") + "/.well-known/openid-configuration"
        )
        if status != 200 or metadata.get("issuer") != issuer:
            raise MemoryError("Identity discovery issuer does not match configuration")
        device_endpoint = _endpoint(metadata.get("device_authorization_endpoint"), issuer)
        token_endpoint = _endpoint(metadata.get("token_endpoint"), issuer)
        status, challenge = _document(
            client,
            "POST",
            device_endpoint,
            data={"client_id": client_id, "scope": " ".join(scopes)},
        )
        if status != 200:
            raise MemoryError("Identity provider rejected device authorization")
        device_code = _secret(challenge.get("device_code"), "device code")
        user_code = challenge.get("user_code")
        if (
            not isinstance(user_code, str)
            or not user_code.strip()
            or not re.fullmatch(r"[A-Za-z0-9 -]{1,128}", user_code)
        ):
            raise MemoryError("Identity provider returned invalid user code")
        verification = _endpoint(challenge.get("verification_uri"), issuer)
        expires = _number(challenge.get("expires_in"), "device lifetime", 1800)
        interval = _number(challenge.get("interval", 5), "poll interval", 60)
        deadline = time.monotonic() + expires
        print(f"Open {verification} and enter code {user_code}", file=sys.stderr, flush=True)
        while True:
            _wait(interval, deadline)
            try:
                status, result = _document(
                    client,
                    "POST",
                    token_endpoint,
                    data={
                        "client_id": client_id,
                        "grant_type": _DEVICE_GRANT,
                        "device_code": device_code,
                    },
                )
            except httpx.TimeoutException:
                interval *= 2
                continue
            if time.monotonic() >= deadline:
                raise MemoryError("Device authorization expired; start login again")
            error = result.get("error")
            if status == 400 and error == "authorization_pending":
                continue
            if status == 400 and error == "slow_down":
                interval += 5
                continue
            if status != 200 or error is not None:
                if error == "access_denied":
                    raise MemoryError("Device authorization was denied")
                if error == "expired_token":
                    raise MemoryError("Device authorization expired; start login again")
                raise MemoryError("Identity provider rejected the device token request")
            if (
                not isinstance(result.get("token_type"), str)
                or result["token_type"].lower() != "bearer"
            ):
                raise MemoryError("Identity provider did not return a bearer access token")
            token = _secret(result.get("access_token"), "access token")
            expires_in = _number(result.get("expires_in"), "access-token lifetime", 604_800)
            break
        status, identity = _document(
            client,
            "GET",
            url.rstrip("/") + "/v1/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        fields = ("authority_id", "user_id", "credential_id")
        if status != 200 or any(
            not isinstance(identity.get(name), str) or not 1 <= len(identity[name]) <= 2000
            for name in fields
        ):
            raise MemoryError("Memory service did not validate the access token")
    _save_token(token_output, token)
    return {
        **{name: identity[name] for name in fields},
        "token_file": str(token_output),
        "expires_in": expires_in,
    }
