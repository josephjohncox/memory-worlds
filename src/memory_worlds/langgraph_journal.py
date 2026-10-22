"""Private durable adapter records, capture intents, and invocation exposure.

This journal belongs to a trusted application runtime, never to model-controlled
graph state. Identifiers record attribution; the adapter must still check the
live credential and scope authorization before every service operation.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from .errors import Conflict
from .errors import MemoryError as InvalidJournal
from .setup_files import (
    _directory,
    _private_directory,
    ensure_private_dir,
    private_read,
    write_private,
)

_MAXIMUM = 1_048_576
_IDENTIFIER = re.compile(r"[a-zA-Z0-9_-]{1,100}\Z")
_DOMAIN = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_GUARDS = threading.local()


def _canonical(value: object) -> bytes:
    def validate(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError
            for child in item.values():
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)
        elif item is not None and not isinstance(item, str | bool | int | float):
            raise ValueError

    try:
        validate(value)
        raw = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise InvalidJournal("adapter journal values must be finite JSON") from None
    if len(raw) > _MAXIMUM:
        raise InvalidJournal("adapter journal record exceeds the size limit")
    return raw


def _decode(raw: bytes) -> dict:
    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    def constant(_value):
        raise ValueError

    try:
        value = json.loads(raw, object_pairs_hook=object_pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidJournal("adapter journal record must contain valid JSON") from None
    if not isinstance(value, dict):
        raise InvalidJournal("adapter journal record must be an object")
    _canonical(value)
    return value


def _read_optional(path: Path) -> bytes | None:
    with _directory(path.parent) as directory:
        try:
            os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
    return private_read(path, maximum=_MAXIMUM, managed=True)


class Journal:
    """One private directory bound to one service authority and credential.

    ``freeze`` invokes its factory only when no record exists. A per-record
    process lock covers creation and fsynced publication. If a remote creation
    succeeds and the process dies before saving its reply, it can leave an
    unused remote context; no capture payload may refer to it until saved.
    """

    def __init__(
        self,
        directory: Path | str,
        *,
        service_url: str,
        authority_id: str,
        credential_id: str,
    ):
        parsed = urlsplit(service_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not _IDENTIFIER.fullmatch(authority_id)
            or not _IDENTIFIER.fullmatch(credential_id)
        ):
            raise InvalidJournal("adapter journal requires a valid service and identity binding")
        self.directory = Path(directory).expanduser().absolute()
        self.binding = {
            "service_url": service_url.rstrip("/"),
            "authority_id": authority_id,
            "credential_id": credential_id,
        }
        ensure_private_dir(self.directory)
        with self._lock("binding"):
            path = self.directory / "binding.json"
            raw = _read_optional(path)
            if raw is None:
                write_private(path, _canonical(self.binding))
            else:
                if _decode(raw) != self.binding:
                    raise Conflict("adapter journal belongs to another service or credential")

    @contextmanager
    def _lock(self, key: str):
        with _directory(self.directory) as directory:
            _private_directory(directory)
            fd = os.open(
                ".lock-" + key,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise InvalidJournal("adapter journal lock must be an owned private regular file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _check_binding(self):
        raw = private_read(self.directory / "binding.json", maximum=_MAXIMUM, managed=True)
        if _decode(raw) != self.binding:
            raise Conflict("adapter journal service or credential binding changed")

    @contextmanager
    def invocation_guard(self, invocation_key: str):
        """Serialize exposure changes with the complete automatic write decision.

        Hold this guard from the exposure snapshot through the acknowledged
        service write. It is reentrant within one thread, including when two
        journal objects refer to the same private directory.
        """
        address = self._address("invocation", invocation_key)
        identity = (os.getpid(), str(self.directory), invocation_key)
        held = getattr(_GUARDS, "held", None)
        if held is None:
            held = _GUARDS.held = set()
        if identity in held:
            yield
            return
        with self._lock("invocation-" + address):
            held.add(identity)
            try:
                self._check_binding()
                yield
            finally:
                held.remove(identity)

    @staticmethod
    def _address(domain: str, key: str) -> str:
        if (
            not isinstance(domain, str)
            or not _DOMAIN.fullmatch(domain)
            or not isinstance(key, str)
            or not key
            or len(key) > 2000
        ):
            raise InvalidJournal("adapter journal record requires a bounded domain and key")
        return hashlib.sha256(_canonical([domain, key])).hexdigest()

    def _read(self, address: str, domain: str, key: str) -> tuple[dict | None, str | None]:
        path = self.directory / (address + ".json")
        raw = _read_optional(path)
        if raw is None:
            return None, None
        record = _decode(raw)
        if (
            set(record) != {"binding", "domain", "key", "parameters", "value"}
            or record["binding"] != self.binding
            or record["domain"] != domain
            or record["key"] != key
            or not isinstance(record["parameters"], dict)
            or not isinstance(record["value"], dict)
        ):
            raise InvalidJournal("adapter journal record identity is invalid")
        return record, hashlib.sha256(raw).hexdigest()

    def get(self, domain: str, key: str) -> dict | None:
        """Read an existing value; absence never creates a new lifecycle record."""
        address = self._address(domain, key)
        with self._lock(address):
            self._check_binding()
            record, _ = self._read(address, domain, key)
            return None if record is None else record["value"]

    def freeze(self, domain: str, key: str, parameters: dict, factory: Callable[[], dict]) -> dict:
        """Save the complete original value before delivery and reject changed input.

        Receipt records use a separate domain and the original intent digest as
        parameters. A saved receipt never replaces live authorization checks.
        """
        if domain == "exposure":
            raise InvalidJournal("exposure records require the monotonic ledger operation")
        if not isinstance(parameters, dict):
            raise InvalidJournal("adapter journal parameters must be an object")
        address = self._address(domain, key)
        normalized = _decode(_canonical(parameters))
        with self._lock(address):
            self._check_binding()
            record, _ = self._read(address, domain, key)
            if record is not None:
                if _canonical(record["parameters"]) != _canonical(normalized):
                    raise Conflict("adapter operation was reused with changed parameters")
                return record["value"]
            value = factory()
            if not isinstance(value, dict):
                raise InvalidJournal("adapter journal factory must return an object")
            record = {
                "binding": self.binding,
                "domain": domain,
                "key": key,
                "parameters": normalized,
                "value": value,
            }
            raw = _canonical(record)
            write_private(self.directory / (address + ".json"), raw)
            return _decode(raw)["value"]

    @staticmethod
    def _qualified(scopes: Iterable[dict]) -> list[dict]:
        qualified = set()
        for scope in scopes:
            if (
                not isinstance(scope, dict)
                or set(scope) != {"authority_id", "world_id", "scope_id"}
                or not isinstance(scope["authority_id"], str)
                or not _IDENTIFIER.fullmatch(scope["authority_id"])
                or not isinstance(scope["world_id"], str)
                or not _IDENTIFIER.fullmatch(scope["world_id"])
            ):
                raise InvalidJournal("exposure requires qualified authority, world and UUID scope")
            try:
                scope_id = str(UUID(str(scope["scope_id"])))
            except (TypeError, ValueError, AttributeError):
                raise InvalidJournal("exposure requires a canonical UUID scope") from None
            qualified.add((scope["authority_id"], scope["world_id"], scope_id))
        return [
            {"authority_id": authority, "world_id": world, "scope_id": scope}
            for authority, world, scope in sorted(qualified)
        ]

    @classmethod
    def _exposure(cls, value: dict) -> dict:
        if (
            set(value) != {"scopes", "authorities", "unknown"}
            or not isinstance(value["scopes"], list)
            or not isinstance(value["authorities"], list)
            or not isinstance(value["unknown"], bool)
            or any(
                not isinstance(authority, str) or not _IDENTIFIER.fullmatch(authority)
                for authority in value["authorities"]
            )
        ):
            raise InvalidJournal("adapter exposure ledger is invalid")
        scopes = cls._qualified(value["scopes"])
        authorities = sorted(set(value["authorities"]) | {s["authority_id"] for s in scopes})
        return {"scopes": scopes, "authorities": authorities, "unknown": value["unknown"]}

    def exposure(self, invocation_key: str) -> dict:
        """Return durable exposure; missing records represent a fresh invocation."""
        value = self.get("exposure", invocation_key)
        if value is None:
            return {"scopes": [], "authorities": [], "unknown": False}
        return self._exposure(value)

    def record_exposure(
        self,
        invocation_key: str,
        *,
        scopes: Iterable[dict] = (),
        authorities: Iterable[str] = (),
        unknown: bool = False,
    ) -> dict:
        """Durably union observed provenance before returning source content.

        The caller must derive observations from authenticated service replies
        or a verified runtime handoff. Model-supplied dictionaries are not proof
        of provenance. Unknown exposure is sticky for the whole invocation.
        """
        addition = self._exposure(
            {"scopes": list(scopes), "authorities": list(authorities), "unknown": unknown}
        )
        address = self._address("exposure", invocation_key)
        with self.invocation_guard(invocation_key), self._lock(address):
            self._check_binding()
            record, previous_hash = self._read(address, "exposure", invocation_key)
            previous = (
                {"scopes": [], "authorities": [], "unknown": False}
                if record is None
                else self._exposure(record["value"])
            )
            value = self._exposure(
                {
                    "scopes": previous["scopes"] + addition["scopes"],
                    "authorities": previous["authorities"] + addition["authorities"],
                    "unknown": previous["unknown"] or addition["unknown"],
                }
            )
            record = {
                "binding": self.binding,
                "domain": "exposure",
                "key": invocation_key,
                "parameters": {},
                "value": value,
            }
            write_private(
                self.directory / (address + ".json"),
                _canonical(record),
                expected_hash=previous_hash,
            )
            return value
