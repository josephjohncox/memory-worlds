"""Explicit-issuer OIDC access tokens with durable, live world authorization.

No identity headers, token-selected key URLs, email-based identity, or administrator
claims are accepted. The operator supplies the issuer, API audience, and JWKS URL.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
import jwt

from memory_worlds.errors import Unauthorized, Unavailable
from memory_worlds.models import Principal, Role
from memory_worlds.store import MemoryStore

_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)
_PREFIX = "MEMORY_WORLDS_OIDC_"


def _trusted_url(value: str) -> None:
    parsed = urlsplit(value)
    # Accessing port validates malformed and out-of-range authorities at startup.
    _ = parsed.port
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ValueError("OIDC URLs cannot contain credentials, query or fragment")
    if parsed.scheme == "https":
        return
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if parsed.scheme != "http" or not loopback:
        raise ValueError("OIDC URLs require HTTPS, except loopback HTTP for development")


@dataclass(frozen=True)
class AuthConfig:
    issuer: str
    audience: str
    jwks_url: str
    algorithms: tuple[str, ...] = ("RS256",)
    groups_claim: str = "groups"
    group_mappings: dict[str, dict[str, Role]] = field(default_factory=dict)
    cache_ttl: float = 300
    refresh_cooldown: float = 5
    request_timeout: float = 5

    def __post_init__(self) -> None:
        _trusted_url(self.issuer)
        _trusted_url(self.jwks_url)
        if not self.audience or len(self.audience) > 2000:
            raise ValueError("OIDC requires a nonempty API audience")
        if not self.algorithms or set(self.algorithms) - _ALGORITHMS:
            raise ValueError("OIDC algorithms must be explicitly supported asymmetric algorithms")
        if not 1 <= self.cache_ttl <= 3600 or not 0 <= self.refresh_cooldown <= 60:
            raise ValueError("OIDC cache TTL must be 1..3600 seconds and cooldown 0..60 seconds")
        if not 0 < self.request_timeout <= 30:
            raise ValueError("OIDC request timeout must be in (0, 30] seconds")
        if not self.groups_claim or len(self.groups_claim) > 100:
            raise ValueError("OIDC groups claim must be nonempty and at most 100 characters")
        if not isinstance(self.group_mappings, dict) or len(self.group_mappings) > 1000:
            raise ValueError("OIDC group mappings must be an object with at most 1000 groups")
        for group, worlds in self.group_mappings.items():
            if not isinstance(group, str) or not group or not isinstance(worlds, dict):
                raise ValueError("Each OIDC group mapping must map a group name to world roles")
            for world, role in worlds.items():
                if not isinstance(world, str) or not world or not isinstance(role, Role):
                    raise ValueError("OIDC group mapping world roles must use Role values")

    @classmethod
    def from_env(cls) -> AuthConfig | None:
        names = ("ISSUER", "AUDIENCE", "JWKS_URL")
        values = {name: os.environ.get(_PREFIX + name) for name in names}
        configured = any(key.startswith(_PREFIX) for key in os.environ)
        if not configured:
            return None
        if not all(values.values()):
            raise ValueError("OIDC requires ISSUER, AUDIENCE and JWKS_URL together")
        try:
            raw = json.loads(os.environ.get(_PREFIX + "GROUP_MAPPINGS", "{}"))
            if not isinstance(raw, dict):
                raise ValueError("OIDC group mappings must be a JSON object")
            mappings = {
                group: {world: Role(role) for world, role in worlds.items()}
                for group, worlds in raw.items()
            }
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("Invalid OIDC group mapping JSON") from error
        return cls(
            issuer=values["ISSUER"],
            audience=values["AUDIENCE"],
            jwks_url=values["JWKS_URL"],
            algorithms=tuple(os.environ.get(_PREFIX + "ALGORITHMS", "RS256").split(",")),
            groups_claim=os.environ.get(_PREFIX + "GROUPS_CLAIM", "groups"),
            group_mappings=mappings,
        )


class OIDCAuthenticator:
    """Synchronous verifier, intended for FastAPI's synchronous dependency thread pool."""

    def __init__(self, store: MemoryStore, config: AuthConfig):
        self.store = store
        self.config = config
        self._lock = threading.Lock()
        self._keys: dict[str, dict] = {}
        self._expires = 0.0
        self._last_attempt = float("-inf")

    def reconcile(self) -> None:
        """Apply operator mapping changes while the authority's daemon lock is held."""
        self.store.reconcile_oidc_mappings(self.config.issuer, self.config.group_mappings)

    def _refresh(self) -> None:
        now = time.monotonic()
        if now - self._last_attempt < self.config.refresh_cooldown:
            return
        self._last_attempt = now
        try:
            # No redirects, environment proxies, or URLs supplied by JWT headers.
            with httpx.stream(
                "GET",
                self.config.jwks_url,
                timeout=self.config.request_timeout,
                follow_redirects=False,
                trust_env=False,
            ) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 1_048_576:
                        raise ValueError("JWKS response exceeds one MiB")
            document = json.loads(content)
            raw_keys = document["keys"]
            if not isinstance(raw_keys, list) or not 1 <= len(raw_keys) <= 64:
                raise ValueError("JWKS must have 1..64 keys")
            keys = {}
            for key in raw_keys:
                if not isinstance(key, dict):
                    raise ValueError("Invalid signing key")
                # Encryption keys may coexist in the provider's published set.
                if key.get("use", "sig") != "sig":
                    continue
                kid = key.get("kid")
                if not isinstance(kid, str) or not kid or len(kid) > 200 or kid in keys:
                    raise ValueError("JWKS signing keys require unique, nonempty key identifiers")
                keys[kid] = key
            self._keys = keys
            self._expires = time.monotonic() + self.config.cache_ttl
        except (httpx.HTTPError, ValueError, TypeError, KeyError, RecursionError) as error:
            raise Unavailable("Identity signing keys are unavailable") from error

    def _key(self, kid: str, algorithm: str, refresh: bool = False) -> jwt.PyJWK:
        with self._lock:
            now = time.monotonic()
            if now >= self._expires or kid not in self._keys or refresh:
                self._refresh()
            if time.monotonic() >= self._expires:
                raise Unavailable("Identity signing keys have expired")
            key = self._keys.get(kid)
            if key is None:
                raise Unauthorized("Unknown identity signing key")
            if key.get("alg", algorithm) != algorithm or "verify" not in key.get(
                "key_ops", ["verify"]
            ):
                raise Unauthorized("Identity signing key does not permit this algorithm")
            try:
                return jwt.PyJWK.from_dict(key, algorithm=algorithm)
            except (jwt.PyJWTError, ValueError, TypeError, KeyError, RecursionError) as error:
                raise Unauthorized("Invalid identity signing key") from error

    def _decode(self, token: str, key: jwt.PyJWK) -> dict:
        return jwt.decode(
            token,
            key=key,
            algorithms=list(self.config.algorithms),
            issuer=self.config.issuer,
            audience=self.config.audience,
            options={"require": ["iss", "aud", "sub", "exp"]},
        )

    def authenticate(self, token: str) -> Principal:
        if len(token) > 16_384:
            raise Unauthorized("Identity token is too large")
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            kid = header.get("kid")
            if algorithm not in self.config.algorithms:
                raise Unauthorized("Identity token uses an unsupported algorithm")
            if not isinstance(kid, str) or not kid or len(kid) > 200:
                raise Unauthorized("Identity token requires a signing key identifier")
            # No critical extensions are supported; never silently ignore one.
            if header.get("crit") or header.get("b64") is False:
                raise Unauthorized("Identity token uses unsupported header extensions")
            key = self._key(kid, algorithm)
            try:
                claims = self._decode(token, key)
            except jwt.InvalidSignatureError:
                claims = self._decode(token, self._key(kid, algorithm, refresh=True))
            subject = claims["sub"]
            if not isinstance(subject, str) or not subject.strip() or len(subject) > 1024:
                raise Unauthorized("Identity token requires a nonempty subject")
            for name in ("exp", "nbf", "iat"):
                value = claims.get(name)
                if name in claims and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                ):
                    raise Unauthorized("Identity token times must be finite NumericDate values")
            if self.config.group_mappings and "iat" not in claims:
                raise Unauthorized("Mapped identity groups require a signed issued-at time")
            groups = claims.get(self.config.groups_claim, [])
            if (
                not isinstance(groups, list)
                or len(groups) > 1000
                or any(not isinstance(group, str) or len(group) > 1000 for group in groups)
            ):
                raise Unauthorized("Identity groups must be a bounded array of strings")
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, RecursionError) as error:
            raise Unauthorized("Identity token is invalid or expired") from error
        return self.store.authenticate_oidc(
            issuer=self.config.issuer,
            subject=subject,
            token_digest=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=claims["exp"],
            groups=groups,
            group_mappings=self.config.group_mappings,
            issued_at=claims.get("iat"),
        )
