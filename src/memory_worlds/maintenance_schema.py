"""Current context archive and derived-artifact storage, registered before copying metadata."""

from sqlalchemy import Column, Index, Integer, Table, Text


def register(metadata):
    from .persistence import SOURCE_METADATA

    if "context_bundles" not in metadata.tables:
        Table(
            "context_bundles",
            metadata,
            Column("id", Text, primary_key=True),
            Column("thread_id", Text, nullable=False),
            Column("credential_id", Text, nullable=False),
            Column("request_digest", Text, nullable=False),
            Column("captured_at", Text, nullable=False),
            Column("state", Text, nullable=False),
            Column("manifest", Text),
            Column("revision", Integer),
        )
        Table(
            "artifact_locations",
            metadata,
            Column("id", Text, primary_key=True),
            Column("thread_id", Text, nullable=False),
            Column("job_id", Text, nullable=False),
            Column("request_digest", Text, nullable=False),
            Column("created_at", Text, nullable=False),
        )
    if "context_artifacts" not in SOURCE_METADATA.tables:
        Table(
            "context_artifacts",
            SOURCE_METADATA,
            Column("id", Text, primary_key=True),
            Column("episode_id", Text, nullable=False, unique=True),
            Column("thread_id", Text, nullable=False),
            Column("job_id", Text, nullable=False),
            Column("kind", Text, nullable=False),
            Column("source_refs", Text, nullable=False),
        )
        dependencies = Table(
            "artifact_dependencies",
            SOURCE_METADATA,
            Column("output_episode_id", Text, primary_key=True),
            Column("input_episode_id", Text, primary_key=True),
            Column("input_digest", Text, nullable=False),
        )
        Index("artifact_dependency_input", dependencies.c.input_episode_id)
