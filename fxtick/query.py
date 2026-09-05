"""SQL and its complete selected inputs travel together through transformations.

Only controlled builders create queries in production. Plain SQL is intentionally
not an export credential. This is an application boundary, not a Python sandbox.
"""
from dataclasses import dataclass
from .artifacts import Artifact, IntegrityError, derive
from .policy import ExportPurpose


@dataclass(frozen=True)
class Query:
    sql: str
    inputs: tuple[Artifact, ...]

    def __str__(self):
        return self.sql

    def wrap(self, sql):
        return Query(sql, self.inputs)

    def check(self, purpose):
        if not self.inputs:
            raise IntegrityError("Query has no verified inputs")
        for artifact in self.inputs:
            artifact.check(purpose)
        lineage = derive(a.lineage for a in self.inputs)
        lineage.check(purpose)
        return lineage


def require_query(query, purpose=ExportPurpose.LOCAL_TEST):
    if not isinstance(query, Query):
        raise IntegrityError("Unbound SQL: use duck.source_sql/normalized_select/union_sources")
    return query.check(purpose)
