import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from memory_worlds.auth import AuthConfig, OIDCAuthenticator
from memory_worlds.errors import Forbidden, Unauthorized, Unavailable
from memory_worlds.models import (
    CreateScope,
    CreateToken,
    CreateWorld,
    DelegateCredential,
    EpisodeInput,
    Role,
    ScopeKind,
    WorldKind,
)
from memory_worlds.store import MemoryStore


@pytest.fixture
def identity(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    _, admin_token = store.bootstrap("operator")
    admin = store.authenticate(admin_token)
    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(signing.public_key()))
    jwk.update(kid="initial", alg="RS256", use="sig")
    state = {"keys": [jwk], "requests": 0, "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"] += 1
            self.send_response(state["status"])
            self.end_headers()
            self.wfile.write(state.get("document", json.dumps({"keys": state["keys"]}).encode()))

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    issuer = f"http://127.0.0.1:{server.server_port}"
    config = AuthConfig(issuer, "memory-api", issuer + "/jwks", refresh_cooldown=0)
    auth = OIDCAuthenticator(store, config)
    auth.reconcile()

    def token(subject="alice", *, key=signing, kid="initial", **changes):
        claims = {
            "iss": issuer,
            "aud": "memory-api",
            "sub": subject,
            "exp": time.time() + 300,
            "iat": time.time(),
        }
        claims.update(changes)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})

    yield store, admin, auth, token, state, signing
    server.shutdown()
    server.server_close()
    thread.join()


def episode(source_id="source"):
    return EpisodeInput(
        source_id=source_id, content="Private evidence", occurred_at=datetime.now(UTC)
    )


def test_oidc_identity_is_stable_private_and_never_an_administrator(identity):
    store, admin, auth, token, _, _ = identity
    alice_token = token(admin=True, email="same@example.test")
    alice = auth.authenticate(alice_token)
    again = auth.authenticate(token(email="changed@example.test"))
    bob = auth.authenticate(token("bob", email="same@example.test"))
    assert again.user_id == alice.user_id != bob.user_id
    assert alice.user_id.startswith("user_")
    assert not alice.admin
    with pytest.raises(Unauthorized):
        store.authenticate(alice_token)
    with pytest.raises(Forbidden):
        store.issue_token(alice, CreateToken(user_id=alice.user_id, agent_id="admin", admin=True))
    alice_world = store.list_worlds(alice)[0]
    bob_world = store.list_worlds(bob)[0]
    assert alice_world.kind == WorldKind.PRIVATE_WORK
    assert alice_world.owner_id == alice.user_id
    receipt = store.capture(alice, alice_world.id, episode())
    for operation in (
        lambda: store.get_world(bob, alice_world.id),
        lambda: store.get_episode(bob, alice_world.id, receipt.episode.id),
        lambda: store.list_episodes(bob, alice_world.id),
        lambda: store.search(bob, [bob_world.id, alice_world.id], "evidence"),
        lambda: store.snapshot(bob, alice_world.id),
        lambda: store.capture(bob, alice_world.id, episode("other")),
        lambda: store.set_membership(admin, alice_world.id, bob.user_id, Role.READER),
    ):
        with pytest.raises(Forbidden):
            operation()
    reopened = MemoryStore(store.data_dir)
    assert reopened.get_episode(alice, alice_world.id, receipt.episode.id) == receipt.episode
    assert len(store.list_users(admin)) == 2
    forged = alice.model_copy(update={"admin": True})
    with pytest.raises(Unauthorized):
        store.validate_principal(forged)


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://untrusted.test"},
        {"aud": "different-api"},
        {"exp": 1},
        {"exp": None},
        {"exp": "99999999999"},
        {"exp": float("inf")},
        {"nbf": 99999999999},
        {"iat": 99999999999},
        {"sub": ""},
        {"sub": " "},
        {"sub": 42},
        {"groups": "admin"},
        {"groups": [42]},
    ],
)
def test_signed_tokens_with_invalid_claims_are_rejected(identity, claims):
    store, admin, auth, token, _, _ = identity
    with pytest.raises(Unauthorized):
        auth.authenticate(token(**claims))
    assert store.list_users(admin) == []


def test_missing_required_claims_signature_algorithms_and_headers_rejected(identity):
    _, _, auth, token, _, signing = identity
    good = jwt.decode(token(), options={"verify_signature": False})
    invalid = []
    for claim in ("sub", "iss", "exp", "aud"):
        claims = {k: v for k, v in good.items() if k != claim}
        invalid.append(jwt.encode(claims, signing, algorithm="RS256", headers={"kid": "initial"}))
    invalid.extend(
        [
            jwt.encode(good, "untrusted" * 8, algorithm="HS256", headers={"kid": "initial"}),
            jwt.encode(good, None, algorithm="none", headers={"kid": "initial"}),
            jwt.encode(good, signing, algorithm="RS256"),
            jwt.encode(good, signing, algorithm="RS256", headers={"kid": "initial", "crit": ["x"]}),
            token(key=rsa.generate_private_key(public_exponent=65537, key_size=2048)),
            "nonsense",
            "x" * 16_385,
        ]
    )
    for invalid_token in invalid:
        with pytest.raises(Unauthorized):
            auth.authenticate(invalid_token)


def test_nested_jwks_is_unavailable_and_a_later_valid_refresh_recovers(identity):
    store, admin, auth, token, state, _ = identity
    state["document"] = b'{"keys":[' + b"[" * 10000 + b"0" + b"]" * 10000 + b"]}"
    with pytest.raises(Unavailable, match="signing keys are unavailable"):
        auth.authenticate(token())
    assert store.list_users(admin) == []
    del state["document"]
    assert auth.authenticate(token()).user_id.startswith("user_")


@pytest.mark.parametrize("part", ["header", "payload"])
def test_deeply_nested_jwt_is_rejected_without_persisting_identity(identity, part):
    store, admin, auth, token, _, signing = identity
    nested = b"[" * 5000 + b"0" + b"]" * 5000
    if part == "header":
        header = b'{"alg":"RS256","kid":"initial","crit":' + nested + b"}"
        compact = jwt.utils.base64url_encode(header) + b".e30.invalid"
        bearer = compact.decode()
    else:
        claims = jwt.decode(token(), options={"verify_signature": False})
        payload = json.dumps(claims).encode()[:-1] + b',"groups":' + nested + b"}"
        bearer = jwt.PyJWS().encode(payload, signing, algorithm="RS256", headers={"kid": "initial"})
    assert len(bearer) < 16384
    with pytest.raises(Unauthorized):
        auth.authenticate(bearer)
    assert store.list_users(admin) == []


def test_membership_changes_affect_existing_oidc_sessions_without_expanding_local_grants(identity):
    store, admin, auth, token, _, _ = identity
    alice = auth.authenticate(token())
    team = store.create_world(admin, CreateWorld(name="Team", kind=WorldKind.TEAM))
    local = store.authenticate(
        store.issue_token(
            admin,
            CreateToken(
                user_id=alice.user_id, agent_id="local-agent", grants={team.id: Role.READER}
            ),
        )
    )
    empty = store.authenticate(
        store.issue_token(admin, CreateToken(user_id=alice.user_id, agent_id="empty-agent"))
    )
    with pytest.raises(Forbidden):
        store.get_world(alice, team.id)
    store.set_membership(admin, team.id, alice.user_id, Role.WRITER)
    receipt = store.capture(alice, team.id, episode(), shared=True)
    assert store.get_episode(local, team.id, receipt.episode.id) == receipt.episode
    with pytest.raises(Forbidden):
        store.capture(local, team.id, episode("reader-cannot-inherit-write"), shared=True)
    with pytest.raises(Forbidden):
        store.get_world(empty, team.id)
    store.set_membership(admin, team.id, alice.user_id, Role.READER)
    with pytest.raises(Forbidden):
        store.capture(alice, team.id, episode("denied"), shared=True)
    with pytest.raises(Forbidden):
        store.set_membership(alice, team.id, alice.user_id, Role.MANAGER)
    store.set_membership(admin, team.id, alice.user_id, None)
    assert store.list_memberships(admin, team.id) == []
    for principal in (alice, empty):
        with pytest.raises(Forbidden):
            store.get_world(principal, team.id)
    # Explicit local grants survive user membership changes until separately revoked.
    assert store.get_episode(local, team.id, receipt.episode.id) == receipt.episode


def test_mapped_memberships_cannot_elevate_or_expand_local_agent_grants(identity):
    store, admin, auth, token, _, _ = identity
    team = store.create_world(admin, CreateWorld(name="Team", kind=WorldKind.TEAM))
    mapped = OIDCAuthenticator(
        store,
        replace(
            auth.config,
            group_mappings={
                "team": {team.id: Role.WRITER},
            },
        ),
    )
    alice = mapped.authenticate(token(groups=["team"]))
    reader = store.authenticate(
        store.issue_token(
            admin,
            CreateToken(user_id=alice.user_id, agent_id="reader", grants={team.id: Role.READER}),
        )
    )
    empty = store.authenticate(
        store.issue_token(admin, CreateToken(user_id=alice.user_id, agent_id="empty"))
    )
    assert store.get_world(reader, team.id).id == team.id
    with pytest.raises(Forbidden):
        store.capture(reader, team.id, episode(), shared=True)
    with pytest.raises(Forbidden):
        store.get_world(empty, team.id)


def test_mapping_restart_removes_stale_grants_before_any_identity_reauthenticates(identity):
    store, admin, auth, token, _, _ = identity
    team = store.create_world(admin, CreateWorld(name="Team", kind=WorldKind.TEAM))
    manual = store.create_world(admin, CreateWorld(name="Manual", kind=WorldKind.AD_HOC))
    config = replace(auth.config, group_mappings={"team": {team.id: Role.WRITER}})
    mapped = OIDCAuthenticator(store, config)
    bearer = token(groups=["team"])
    alice = mapped.authenticate(bearer)
    store.set_membership(admin, manual.id, alice.user_id, Role.READER)
    OIDCAuthenticator(store, config).reconcile()  # Unchanged map keeps accepted memberships.
    assert store.get_world(alice, team.id).id == team.id
    changed = OIDCAuthenticator(
        store,
        replace(
            config,
            group_mappings={
                "team": {team.id: Role.READER},
            },
        ),
    )
    # Merely preparing a second app must not mutate the active authority.
    assert store.get_world(alice, team.id).id == team.id
    changed.reconcile()
    with pytest.raises(Forbidden):
        store.get_world(alice, team.id)
    assert store.get_world(alice, manual.id).id == manual.id
    changed.authenticate(bearer)  # Reuse the accepted claims without advancing their iat.
    assert store.get_world(alice, team.id).id == team.id
    with pytest.raises(Forbidden):
        store.capture(alice, team.id, episode(), shared=True)
    OIDCAuthenticator(store, replace(config, group_mappings={})).reconcile()
    with pytest.raises(Forbidden):
        store.get_world(alice, team.id)
    assert store.get_world(alice, manual.id).id == manual.id


@pytest.mark.parametrize("active", [None, "https://another-issuer.test"])
def test_inactive_issuer_sessions_are_revoked_without_revoking_local_credentials(identity, active):
    store, admin, auth, token, _, _ = identity
    bearer = token()
    alice = auth.authenticate(bearer)
    local = store.authenticate(
        store.issue_token(admin, CreateToken(user_id=alice.user_id, agent_id="local"))
    )
    store.reconcile_oidc_issuer(active)
    with pytest.raises(Unauthorized):
        store.validate_principal(alice)
    with pytest.raises(Unauthorized):
        auth.authenticate(bearer)
    assert store.validate_principal(local) == local


def test_revoked_session_cannot_reauthenticate_and_user_disable_revokes_all_agents(identity):
    store, admin, auth, token, _, _ = identity
    bearer = token()
    alice = auth.authenticate(bearer)
    store.revoke_token(admin, alice.credential_id)
    with pytest.raises(Unauthorized):
        auth.authenticate(bearer)
    with pytest.raises(Unauthorized):
        store.list_worlds(alice)
    later_bearer = token(jti="new-login")
    later = auth.authenticate(later_bearer)
    local = store.authenticate(
        store.issue_token(admin, CreateToken(user_id=later.user_id, agent_id="agent"))
    )
    store.set_user_enabled(admin, later.user_id, False)
    for principal in (later, local):
        with pytest.raises(Unauthorized):
            store.validate_principal(principal)
    with pytest.raises(Unauthorized):
        auth.authenticate(token(jti="still-disabled"))
    store.set_user_enabled(admin, later.user_id, True)
    with pytest.raises(Unauthorized):
        auth.authenticate(later_bearer)
    restored = auth.authenticate(token(jti="after-enable"))
    assert restored.user_id == later.user_id


def test_store_rechecks_session_expiration_without_reverifying_jwt(identity, monkeypatch):
    store, _, auth, token, _, _ = identity
    alice = auth.authenticate(token())
    world = store.list_worlds(alice)[0]
    monkeypatch.setattr("memory_worlds.store.time.time", lambda: 99999999999)
    with pytest.raises(Unauthorized):
        store.get_world(alice, world.id)


def test_issuer_subject_pair_not_email_controls_identity(identity):
    store, admin, auth, token, _, signing = identity
    first = auth.authenticate(token(email="shared@example.test"))
    other_config = replace(auth.config, issuer=auth.config.issuer + "/other")
    other = OIDCAuthenticator(store, other_config)
    claims = jwt.decode(token(), options={"verify_signature": False})
    claims.update(iss=other_config.issuer, email="shared@example.test")
    bearer = jwt.encode(claims, signing, algorithm="RS256", headers={"kid": "initial"})
    assert other.authenticate(bearer).user_id != first.user_id
    assert len(store.list_users(admin)) == 2


def test_jwks_cache_rotation_and_expiry_fail_closed(identity):
    _, _, auth, token, state, _ = identity
    auth.authenticate(token())
    auth.authenticate(token(jti="same-key"))
    assert state["requests"] == 1
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = json.loads(RSAAlgorithm.to_jwk(rotated.public_key()))
    key.update(kid="rotated", alg="RS256")
    state["keys"] = [key]
    auth.authenticate(token(key=rotated, kid="rotated"))
    assert state["requests"] == 2
    state["status"] = 503
    auth.authenticate(token(key=rotated, kid="rotated", jti="cached"))
    auth._expires = 0
    with pytest.raises(Unavailable):
        auth.authenticate(token(key=rotated, kid="rotated"))


def test_unknown_key_refresh_is_rate_limited(identity):
    store, _, auth, token, state, _ = identity
    auth = OIDCAuthenticator(store, replace(auth.config, refresh_cooldown=5))
    auth.authenticate(token())
    for index in range(5):
        with pytest.raises(Unauthorized):
            auth.authenticate(token(kid=f"unknown-{index}"))
    assert state["requests"] == 1


def test_duplicate_key_identifiers_and_wrong_key_usage_are_rejected(identity):
    _, _, auth, token, state, _ = identity
    state["keys"] *= 2
    with pytest.raises(Unavailable):
        auth.authenticate(token())
    state["keys"] = state["keys"][:1]
    state["keys"][0]["key_ops"] = ["decrypt"]
    with pytest.raises(Unauthorized):
        auth.authenticate(token())


def test_operator_mapped_groups_only_and_removals_apply_to_old_sessions(identity):
    store, admin, auth, token, _, _ = identity
    team = store.create_world(admin, CreateWorld(name="Engineering", kind=WorldKind.TEAM))
    company = store.create_world(admin, CreateWorld(name="Company", kind=WorldKind.COMPANY))
    mapped = OIDCAuthenticator(
        store,
        replace(
            auth.config,
            group_mappings={
                "engineering": {team.id: Role.WRITER},
                "employees": {company.id: Role.READER},
            },
        ),
    )
    previous = token(groups=["engineering", "employees", "admin"])
    alice = mapped.authenticate(previous)
    assert set(w.id for w in store.list_worlds(alice)) >= {team.id, company.id}
    store.capture(alice, team.id, episode(), shared=True)
    with pytest.raises(Forbidden):
        store.capture(alice, company.id, episode(), shared=True)
    mapped.authenticate(token(groups=["employees"], jti="updated"))
    with pytest.raises(Unauthorized):
        mapped.authenticate(previous)
    with pytest.raises(Forbidden):
        store.get_world(alice, team.id)
    bob = mapped.authenticate(token("bob", groups=[team.id, "admin"]))
    with pytest.raises(Forbidden):
        store.get_world(bob, team.id)
    private = store.list_worlds(bob)[0]
    invalid = OIDCAuthenticator(
        store,
        replace(
            auth.config,
            group_mappings={
                "bad": {private.id: Role.READER},
            },
        ),
    )
    with pytest.raises(Forbidden):
        invalid.authenticate(token(groups=["bad"]))


def test_configuration_is_explicit_and_unsafe_algorithms_urls_rejected(monkeypatch):
    for key in tuple(__import__("os").environ):
        if key.startswith("MEMORY_WORLDS_OIDC_"):
            monkeypatch.delenv(key)
    assert AuthConfig.from_env() is None
    monkeypatch.setenv("MEMORY_WORLDS_OIDC_ISSUER", "https://identity.test")
    with pytest.raises(ValueError):
        AuthConfig.from_env()
    monkeypatch.setenv("MEMORY_WORLDS_OIDC_AUDIENCE", "api")
    monkeypatch.setenv("MEMORY_WORLDS_OIDC_JWKS_URL", "https://identity.test/jwks")
    monkeypatch.setenv("MEMORY_WORLDS_OIDC_GROUP_MAPPINGS", '{"team": {"world": "reader"}}')
    assert AuthConfig.from_env().group_mappings == {"team": {"world": Role.READER}}
    for changes in (
        {"algorithms": ("HS256",)},
        {"issuer": "http://remote.test"},
        {"jwks_url": "https://id.test/jwks?query=1"},
        {"jwks_url": "https://user:password@id.test/jwks"},
        {"issuer": "https://id.test\n"},
    ):
        with pytest.raises(ValueError):
            replace(AuthConfig.from_env(), **changes)


def test_group_watermark_rejects_unseen_older_and_equal_time_conflicting_tokens(identity):
    store, admin, auth, token, _, _ = identity
    team = store.create_world(admin, CreateWorld(name="Team", kind=WorldKind.TEAM))
    mapped = OIDCAuthenticator(
        store,
        replace(
            auth.config,
            group_mappings={
                "members": {team.id: Role.READER},
            },
        ),
    )
    issued_at = time.time() - 30
    old_unseen = token(groups=["members"], iat=issued_at, jti="unseen-old")
    alice = mapped.authenticate(token(groups=["members"], iat=issued_at))
    assert store.get_world(alice, team.id).id == team.id
    newer = token(groups=[], iat=issued_at + 10)
    mapped.authenticate(newer)
    for rejected in (
        old_unseen,
        token(groups=["members"], iat=issued_at + 10, jti="same-time-conflict"),
    ):
        with pytest.raises(Unauthorized):
            mapped.authenticate(rejected)
        with pytest.raises(Forbidden):
            store.get_world(alice, team.id)
    # Same claims with a new token/session are harmless; watermark survives restart.
    restarted = OIDCAuthenticator(MemoryStore(store.data_dir), mapped.config)
    restarted.authenticate(token(groups=[], iat=issued_at + 10, jti="same-groups"))
    with pytest.raises(Unauthorized):
        restarted.authenticate(old_unseen)


def test_mapped_groups_require_iat_and_operator_map_changes_apply_on_authentication(identity):
    store, admin, auth, token, _, signing = identity
    team = store.create_world(admin, CreateWorld(name="Team", kind=WorldKind.TEAM))
    config = replace(auth.config, group_mappings={"members": {team.id: Role.READER}})
    mapped = OIDCAuthenticator(store, config)
    no_iat = jwt.decode(token(groups=["members"]), options={"verify_signature": False})
    del no_iat["iat"]
    with pytest.raises(Unauthorized):
        mapped.authenticate(
            jwt.encode(no_iat, signing, algorithm="RS256", headers={"kid": "initial"})
        )
    bearer = token(groups=["members"])
    alice = mapped.authenticate(bearer)
    store.set_membership(admin, team.id, alice.user_id, Role.READER)
    changed_config = replace(config, group_mappings={"different-group": {team.id: Role.WRITER}})
    changed = OIDCAuthenticator(store, changed_config)
    changed.authenticate(bearer)
    memberships = store.list_memberships(admin, team.id)
    assert memberships == [
        {
            "user_id": alice.user_id,
            "world_id": team.id,
            "role": "reader",
            "source": "manual",
        }
    ]
    # Explicit user membership remains even when the operator removes the group map.
    store.set_membership(admin, team.id, alice.user_id, None)
    with pytest.raises(Forbidden):
        store.get_world(alice, team.id)


def test_application_and_scope_policy_uses_verified_claims_without_granting_worlds(identity):
    store, _, auth, token, _, _ = identity
    configured = OIDCAuthenticator(
        store,
        replace(
            auth.config,
            client_ids=("native-client",),
            required_scopes=("memory-worlds",),
        ),
    )
    accepted = configured.authenticate(token(cid="native-client", scp=["openid", "memory-worlds"]))
    assert [world.kind for world in store.list_worlds(accepted)] == [WorldKind.PRIVATE_WORK]
    for claims in (
        {"scp": ["memory-worlds"]},
        {"cid": "other-client", "scp": ["memory-worlds"]},
        {"cid": ["native-client"], "scp": ["memory-worlds"]},
        {"cid": "native-client"},
        {"cid": "native-client", "scp": ["openid"]},
        {"cid": "native-client", "scp": ["memory-worlds", 3]},
        {"cid": "native-client", "scp": "memory-worlds\nadmin"},
    ):
        with pytest.raises(Unauthorized):
            configured.authenticate(token(**claims))
    custom = OIDCAuthenticator(
        store,
        replace(
            configured.config,
            client_id_claim="azp",
            scopes_claim="scope",
        ),
    )
    assert (
        custom.authenticate(token(azp="native-client", scope="openid memory-worlds")).user_id
        == accepted.user_id
    )
    with pytest.raises(Unauthorized):
        custom.authenticate(token(cid="native-client", scp=["memory-worlds"]))


def test_oidc_policy_configuration_and_private_map_targets_fail_closed(identity, monkeypatch):
    store, admin, auth, _, _, _ = identity
    for suffix, value in {
        "ISSUER": auth.config.issuer,
        "AUDIENCE": auth.config.audience,
        "JWKS_URL": auth.config.jwks_url,
        "CLIENT_IDS": "native-client,another-client",
        "CLIENT_ID_CLAIM": "azp",
        "REQUIRED_SCOPES": "memory-worlds audit",
        "SCOPES_CLAIM": "scope",
    }.items():
        monkeypatch.setenv("MEMORY_WORLDS_OIDC_" + suffix, value)
    config = AuthConfig.from_env()
    assert config.client_ids == ("native-client", "another-client")
    assert config.required_scopes == ("memory-worlds", "audit")
    assert config.client_id_claim == "azp" and config.scopes_claim == "scope"
    for changes in (
        {"client_ids": (auth.config.audience,)},
        {"client_ids": ("bad client",)},
        {"required_scopes": ("scope with spaces",)},
        {"client_id_claim": ""},
        {"scopes_claim": "x" * 101},
    ):
        with pytest.raises(ValueError):
            replace(auth.config, **changes)
    private = store.list_worlds(admin)[0]
    for world_id in ("missing-world", private.id):
        mapped = OIDCAuthenticator(
            store, replace(auth.config, group_mappings={"nobody": {world_id: Role.READER}})
        )
        with pytest.raises(ValueError, match="existing shared world IDs"):
            mapped.reconcile()


def test_verifier_policy_change_revokes_sessions_and_private_delegation(identity):
    store, _, auth, token, _, _ = identity
    bearer = token(cid="native-client", scp=["memory-worlds"])
    parent = auth.authenticate(bearer)
    world = store.list_worlds(parent)[0]
    child = store.delegate_credential(parent, DelegateCredential(private_world_id=world.id))
    child_principal = store.authenticate(child["token"])
    auth.reconcile()
    assert store.validate_principal(parent) == parent
    assert store.validate_principal(child_principal) == child_principal
    changed = OIDCAuthenticator(store, replace(auth.config, client_ids=("native-client",)))
    changed.reconcile()
    for principal in (parent, child_principal):
        with pytest.raises(Unauthorized):
            store.validate_principal(principal)
    with pytest.raises(Unauthorized):
        changed.authenticate(bearer)
    fresh = changed.authenticate(token(cid="native-client", scp=["memory-worlds"], jti="fresh"))
    assert fresh.user_id == parent.user_id


def test_finite_far_future_oidc_expiry_preserves_bounded_default_child_lifetime(identity):
    store, _, auth, token, _, _ = identity
    parent = auth.authenticate(token(exp=1e100))
    world = store.list_worlds(parent)[0]
    before = time.time()
    child = store.delegate_credential(parent, DelegateCredential(private_world_id=world.id))
    assert before + 3599 <= child["expires_at"].timestamp() <= time.time() + 3600
    assert store.authenticate(child["token"]).invocation_id == child["context"].invocation_id


def test_signed_oidc_submicrosecond_expiry_caps_child_without_rounding_up(identity):
    store, _, auth, token, _, _ = identity
    expiry = int(time.time()) + 300 + 0.00000075
    assert datetime.fromtimestamp(expiry, UTC).timestamp() > expiry
    parent = auth.authenticate(token(exp=expiry))
    world = store.list_worlds(parent)[0]
    child = store.delegate_credential(parent, DelegateCredential(private_world_id=world.id))
    assert 0 <= expiry - child["expires_at"].timestamp() < 0.000001
    assert store.authenticate(child["token"]).invocation_id == child["context"].invocation_id


def test_shared_group_membership_never_grants_private_scope_access(identity):
    store, operator, auth, token, _, _ = identity
    team = store.create_world(operator, CreateWorld(name="Data", kind=WorldKind.TEAM))
    mapped = OIDCAuthenticator(
        store, replace(auth.config, group_mappings={"data": {team.id: Role.WRITER}})
    )
    alice = mapped.authenticate(token(groups=["data"]))
    bob = mapped.authenticate(token("bob", groups=["data"]))
    scratch = store.create_scope(
        alice, team.id, CreateScope(kind=ScopeKind.INVOCATION_PRIVATE, name="Alice scratch")
    )
    receipt = store.capture(alice, team.id, episode().model_copy(update={"scope_id": scratch.id}))
    assert store.get_episode(alice, team.id, receipt.episode.id, scope_id=scratch.id)
    for other in (bob, operator):
        with pytest.raises(Forbidden):
            store.get_episode(other, team.id, receipt.episode.id, scope_id=scratch.id)
