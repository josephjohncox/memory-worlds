from uuid import uuid4

import pytest

from memory_worlds.errors import Conflict, Forbidden, NotFound, Unauthorized
from memory_worlds.identity_names import CreateIdentity, IdentityRegistry, UpdateIdentity
from memory_worlds.models import BindContext, CreateToken, DelegateCredential
from memory_worlds.store import MemoryStore


@pytest.fixture
def names(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    world, token = store.bootstrap("alice")
    owner = store.authenticate(token)
    return store, world, owner, IdentityRegistry(store)


def test_named_agent_reused_across_distinct_invocations_and_runs(names):
    store, world, owner, registry = names
    agent = registry.create(owner, "agent", CreateIdentity(name="Researcher", alias="research"))
    run = registry.create(owner, "run", CreateIdentity(name="Review", alias="review"))
    contexts = [
        store.bind_context(owner, BindContext(agent_id=agent.id, run_id=run.id)) for _ in range(2)
    ]
    assert contexts[0].id != contexts[1].id
    assert contexts[0].invocation_id != contexts[1].invocation_id
    assert contexts[0].agent_id == contexts[1].agent_id == agent.id
    assert contexts[0].run_id == contexts[1].run_id == run.id
    invocation = registry.create(
        owner,
        "invocation",
        CreateIdentity(
            id=contexts[0].invocation_id, name="Analysis", agent_id=agent.id, run_id=run.id
        ),
    )
    restarted = IdentityRegistry(MemoryStore(store.data_dir))
    assert restarted.resolve(owner, "agent", "@research") == agent
    assert restarted.get(owner, "invocation", invocation.id) == invocation
    assert (
        restarted.create(
            owner, "agent", CreateIdentity(id=agent.id, name=agent.name, alias=agent.alias)
        )
        == agent
    )
    assert store.list_scopes(owner, world.id)[0].kind == "world-shared"


def test_aliases_are_live_owner_scoped_and_never_authority(names):
    store, world, owner, registry = names
    alice = registry.create(owner, "agent", CreateIdentity(name="A", alias="worker"))
    bob_token = store.issue_token(owner, CreateToken(user_id="bob", agent_id="bob", grants={}))
    bob = store.authenticate(bob_token)
    other = registry.create(bob, "agent", CreateIdentity(name="B", alias="worker"))
    assert registry.resolve(owner, "agent", "@worker") == alice
    assert registry.resolve(bob, "agent", "@worker") == other
    with pytest.raises(NotFound):
        registry.get(bob, "agent", alice.id)
    with pytest.raises(Forbidden):
        store.get_world(bob, world.id)
    with pytest.raises(Conflict):
        registry.create(owner, "agent", CreateIdentity(name="C", alias="worker"))
    registry.update(owner, "agent", alice.id, UpdateIdentity(alias="renamed"))
    with pytest.raises(NotFound):
        registry.resolve(owner, "agent", "@worker")
    assert registry.resolve(owner, "agent", "@renamed").id == alice.id
    assert registry.update(owner, "agent", alice.id, UpdateIdentity(alias=None)).alias is None
    assert registry.resolve(owner, "agent", str(alice.id)).id == alice.id


def test_parentage_and_names_cannot_escape_owner_or_delegated_ceiling(names):
    store, world, owner, registry = names
    parent = registry.create(owner, "agent", CreateIdentity(name="Parent"))
    child = registry.create(owner, "agent", CreateIdentity(name="Child", parent_id=parent.id))
    assert child.parent_id == parent.id
    run = registry.create(owner, "run", CreateIdentity(name="Run"))
    issued = store.delegate_credential(
        owner, DelegateCredential(private_world_id=world.id, agent_id=child.id, run_id=run.id)
    )
    principal = store.authenticate(issued["token"])
    assert registry.list(principal, "agent") == [child]
    assert registry.list(principal, "run") == []
    with pytest.raises(NotFound):
        registry.get(principal, "agent", parent.id)
    for operation in (
        lambda: registry.create(principal, "agent", CreateIdentity(name="escape")),
        lambda: registry.update(principal, "agent", child.id, UpdateIdentity(name="renamed")),
    ):
        with pytest.raises(Forbidden):
            operation()
    with pytest.raises(NotFound):
        registry.create(owner, "invocation", CreateIdentity(name="unknown", agent_id=uuid4()))
    store.revoke_token(owner, principal.credential_id)
    with pytest.raises(Unauthorized):
        registry.get(principal, "agent", child.id)


def test_replay_rejects_changed_identity_attributes(names):
    _, _, owner, registry = names
    original = registry.create(owner, "agent", CreateIdentity(name="Original"))
    with pytest.raises(Conflict):
        registry.create(owner, "agent", CreateIdentity(id=original.id, name="Changed"))
    with pytest.raises(Conflict):
        registry.create(owner, "run", CreateIdentity(id=original.id, name="Original"))
