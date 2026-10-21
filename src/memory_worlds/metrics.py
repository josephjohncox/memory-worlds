"""Bounded Prometheus metrics without user, scope, source or credential labels."""

import threading
from collections import Counter

from sqlalchemy import func, select

BUCKETS = (0.01, 0.05, 0.1, 0.5, 1, 5, 30, float("inf"))


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._requests, self._buckets = Counter(), Counter()
        self._duration = 0.0

    def observe(self, method: str, status: int, elapsed: float):
        method = method if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"} else "OTHER"
        with self._lock:
            self._requests[method, f"{status // 100}xx"] += 1
            self._duration += elapsed
            for bound in BUCKETS:
                if elapsed <= bound:
                    self._buckets[bound] += 1

    def render(self, store, *, ready: bool, workers: int) -> str:
        lines = ["# TYPE memory_worlds_http_requests_total counter"]
        with self._lock:
            for (method, status), count in sorted(self._requests.items()):
                lines.append(
                    f'memory_worlds_http_requests_total{{method="{method}",'
                    f'status="{status}"}} {count}'
                )
            lines.append("# TYPE memory_worlds_http_duration_seconds histogram")
            for bound in BUCKETS:
                label = "+Inf" if bound == float("inf") else str(bound)
                lines.append(
                    f'memory_worlds_http_duration_seconds_bucket{{le="{label}"}} '
                    f"{self._buckets[bound]}"
                )
            lines.append(f"memory_worlds_http_duration_seconds_sum {self._duration}")
            lines.append(
                f"memory_worlds_http_duration_seconds_count {sum(self._requests.values())}"
            )
        lines.extend(
            [
                "# TYPE memory_worlds_ready gauge",
                f"memory_worlds_ready {int(ready)}",
                "# TYPE memory_worlds_workers gauge",
                f"memory_worlds_workers {workers}",
            ]
        )
        counts = Counter()
        with store.persistence.operation_guard():
            with store._control() as db:
                scopes = store._table(db, "scopes")
                selected = db.execute(select(scopes.c.world_id, scopes.c.id)).all()
                generations = store._table(db, "projection_generations")
                for state, count in db.execute(
                    select(generations.c.state, func.count()).group_by(generations.c.state)
                ):
                    if state in {"building", "active", "failed", "retired", "purged"}:
                        counts["generation", state] += count
            for world_id, scope_id in selected:
                with store._world_db(world_id, scope_id) as db:
                    for name, label in (
                        ("projection_jobs", "projection"),
                        ("graph_purges", "purge"),
                    ):
                        table = store._table(db, name)
                        for state, count in db.execute(
                            select(table.c.state, func.count()).group_by(table.c.state)
                        ):
                            if state in {
                                "pending",
                                "processing",
                                "projected",
                                "unknown",
                                "failed",
                                "purged",
                            }:
                                counts[label, state] += count
            with store.persistence.outbox() as db:
                table = store._table(db, "deliveries")
                for state, count in db.execute(
                    select(table.c.state, func.count()).group_by(table.c.state)
                ):
                    if state in {"pending", "processing", "delivered", "blocked"}:
                        counts["delivery", state] += count
        lines.append("# TYPE memory_worlds_records gauge")
        for (kind, state), count in sorted(counts.items()):
            lines.append(f'memory_worlds_records{{kind="{kind}",state="{state}"}} {count}')
        return "\n".join(lines) + "\n"
