"""Owner-scoped names for canonical agent, run and invocation identities.

Names describe identities. They confer no world, scope or delegation rights.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field
from sqlalchemy import Column, String, Table, UniqueConstraint, and_, select
from sqlalchemy.exc import IntegrityError

from .errors import Conflict, Forbidden, NotFound
from .errors import MemoryError as InvalidRequest
from .models import Alias, Identifier, Model, Principal
from .persistence import CONTROL_METADATA

IdentityKind = Literal["agent", "run", "invocation"]

Table(
    "named_identities",
    CONTROL_METADATA,
    Column("id", String(36), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("owner_id", String(100), nullable=False),
    Column("name", String(200), nullable=False),
    Column("alias", String(64)),
    Column("created_at", String(64), nullable=False),
    Column("parent_id", String(36)),
    Column("agent_id", String(36)),
    Column("run_id", String(36)),
    UniqueConstraint("owner_id", "kind", "alias"),
)


class CreateIdentity(Model):
    id: UUID | None = None
    name: str = Field(min_length=1, max_length=200)
    alias: Alias | None = None
    parent_id: UUID | None = None
    agent_id: UUID | None = None
    run_id: UUID | None = None


class UpdateIdentity(Model):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    alias: Alias | None = None


class NamedIdentity(CreateIdentity):
    id: UUID
    kind: IdentityKind
    owner_id: Identifier
    created_at: datetime


class IdentityRegistry:
    def __init__(self, store):
        self.store = store
        store.persistence.ensure_control_tables()

    @staticmethod
    def _kind(kind: str) -> IdentityKind:
        if kind not in {"agent", "run", "invocation"}:
            raise NotFound("Identity kind not found")
        return kind

    @staticmethod
    def _restricted_id(principal: Principal, kind: IdentityKind):
        return {"agent": principal.agent_uuid, "invocation": principal.invocation_id}.get(kind)

    def _row(self, db, principal: Principal, kind: IdentityKind, identity_id: UUID):
        table = self.store._table(db, "named_identities")
        if principal.restricted and self._restricted_id(principal, kind) != identity_id:
            raise NotFound("Identity not found")
        row = (
            db.execute(
                select(table).where(
                    table.c.id == str(identity_id),
                    table.c.kind == kind,
                    table.c.owner_id == principal.user_id,
                )
            )
            .mappings()
            .fetchone()
        )
        if row is None:
            raise NotFound("Identity not found")
        return row

    def list(self, principal: Principal, kind: IdentityKind) -> list[NamedIdentity]:
        kind = self._kind(kind)
        with self.store._control() as db:
            self.store._principal(db, principal)
            table = self.store._table(db, "named_identities")
            condition = and_(table.c.kind == kind, table.c.owner_id == principal.user_id)
            if principal.restricted:
                own = self._restricted_id(principal, kind)
                if own is None:
                    return []
                condition = and_(condition, table.c.id == str(own))
            rows = (
                db.execute(
                    select(table)
                    .where(condition)
                    .order_by(
                        table.c.created_at,
                        table.c.id,
                    )
                    .limit(1000)
                )
                .mappings()
                .fetchall()
            )
            return [NamedIdentity.model_validate(dict(row)) for row in rows]

    def get(self, principal: Principal, kind: IdentityKind, identity_id: UUID) -> NamedIdentity:
        kind = self._kind(kind)
        with self.store._control() as db:
            self.store._principal(db, principal)
            return NamedIdentity.model_validate(dict(self._row(db, principal, kind, identity_id)))

    def create(
        self,
        principal: Principal,
        kind: IdentityKind,
        body: CreateIdentity,
    ) -> NamedIdentity:
        kind = self._kind(kind)
        identity_id = body.id or uuid4()
        if kind != "invocation" and (body.agent_id is not None or body.run_id is not None):
            raise InvalidRequest("Agent/run references belong only to invocation identities")
        try:
            with self.store._control() as db, self.store._transaction(db):
                self.store._principal(db, principal)
                if principal.restricted:
                    raise Forbidden("Restricted invocations cannot register independent identities")
                table = self.store._table(db, "named_identities")
                prior = db.execute(select(table).where(table.c.id == str(identity_id)))
                old = prior.mappings().fetchone()
                if old is not None:
                    result = NamedIdentity.model_validate(dict(old))
                    expected = body.model_dump(exclude={"id"})
                    if (
                        result.kind != kind
                        or result.owner_id != principal.user_id
                        or (result.model_dump(include=set(expected)) != expected)
                    ):
                        raise Conflict(
                            "Identity ID is already registered with different attributes"
                        )
                    return result
                if body.parent_id is not None:
                    if body.parent_id == identity_id:
                        raise InvalidRequest("An identity cannot name itself as parent")
                    self._row(db, principal, kind, body.parent_id)
                if body.agent_id is not None:
                    self._row(db, principal, "agent", body.agent_id)
                if body.run_id is not None:
                    self._row(db, principal, "run", body.run_id)
                result = NamedIdentity(
                    **body.model_dump(exclude={"id"}),
                    id=identity_id,
                    kind=kind,
                    owner_id=principal.user_id,
                    created_at=datetime.now(UTC),
                )
                values = result.model_dump(mode="json")
                db.execute(table.insert().values(**values))
                return result
        except IntegrityError:
            raise Conflict("Identity ID or alias is already registered") from None

    def update(
        self,
        principal: Principal,
        kind: IdentityKind,
        identity_id: UUID,
        body: UpdateIdentity,
    ) -> NamedIdentity:
        kind = self._kind(kind)
        values = body.model_dump(exclude_unset=True)
        if "name" in values and values["name"] is None:
            raise InvalidRequest("Identity name cannot be null")
        try:
            with self.store._control() as db, self.store._transaction(db):
                self.store._principal(db, principal)
                if principal.restricted:
                    raise Forbidden("Restricted invocations cannot rename identities")
                old = self._row(db, principal, kind, identity_id)
                table = self.store._table(db, "named_identities")
                if values:
                    db.execute(
                        table.update().where(table.c.id == str(identity_id)).values(**values)
                    )
                return NamedIdentity.model_validate({**dict(old), **values})
        except IntegrityError:
            raise Conflict("Identity alias is already registered") from None

    def resolve(self, principal: Principal, kind: IdentityKind, reference: str) -> NamedIdentity:
        if not reference.startswith("@"):
            try:
                identity_id = UUID(reference)
            except ValueError:
                raise NotFound("Identity not found") from None
            return self.get(principal, kind, identity_id)
        kind = self._kind(kind)
        with self.store._control() as db:
            self.store._principal(db, principal)
            table = self.store._table(db, "named_identities")
            condition = and_(
                table.c.kind == kind,
                table.c.owner_id == principal.user_id,
                table.c.alias == reference[1:],
            )
            if principal.restricted:
                own = self._restricted_id(principal, kind)
                if own is None:
                    raise NotFound("No accessible identity matches that alias")
                condition = and_(condition, table.c.id == str(own))
            row = db.execute(select(table).where(condition)).mappings().fetchone()
            if row is None:
                raise NotFound("No accessible identity matches that alias")
            return NamedIdentity.model_validate(dict(row))


def validate_run(store, db, principal: Principal, run_id: UUID) -> bool:
    """A named owner run supplies identity, never additional scope authority."""
    if principal.restricted:
        return False
    table = store._table(db, "named_identities")
    return (
        db.execute(
            select(table.c.id).where(
                table.c.id == str(run_id),
                table.c.kind == "run",
                table.c.owner_id == principal.user_id,
            )
        ).scalar()
        is not None
    )
