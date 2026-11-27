"""Embeddings in a column, and the nearest rows to one: `vector(dim)` (pgvector) on Postgres, JSON text on SQLite, the same model on both so the unit tests hold real vectors.

A small copy of the design of `strata_python.db.vectors` (this repo is still on the `omarchy-db` package, whose test fixtures clash with the newer strata-python's database
plugin, so strata-python cannot be moved forward here yet); replace it with that module when the repo moves to `strata_python.db`.

    embedding: Mapped[list[float]] = mapped_column(Vector(1024))
    __table_args__ = (hnsw_index("ix_x_embedding_hnsw", "embedding"),)
    hits = nearest(session, X.embedding, query_vector, k=10, where=[X.account == "main"])   # [(row, cosine distance)], closest first

The extension lives in schema `public` (created once per database by its superuser; the migrations do `CREATE EXTENSION IF NOT EXISTS vector SCHEMA public`), and the type and operator
are named with that schema, so they work from a connection whose search_path is the app's schema alone."""

import json
import math
from collections.abc import Iterable, Sequence
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.types import TypeDecorator, UserDefinedType

SCHEMA = "public"


class _PgVector(UserDefinedType[list[float]]):
    cache_ok = True

    def __init__(self, dim: int | None) -> None:
        self.dim = dim

    def get_col_spec(self, **kw: Any) -> str:
        return f"{SCHEMA}.vector({self.dim})" if self.dim else f"{SCHEMA}.vector"


class Vector(TypeDecorator[list[float]]):
    """A list of floats: `vector(dim)` on Postgres, JSON text elsewhere; `dim` is checked on the way in."""

    impl = sa.Text
    cache_ok = True

    def __init__(self, dim: int | None = None) -> None:
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect: Dialect) -> sa.types.TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(_PgVector(self.dim))
        return dialect.type_descriptor(sa.Text())

    def process_bind_param(self, value: Sequence[float] | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        v = [float(x) for x in value]
        if self.dim and len(v) != self.dim:
            raise ValueError(f"a vector of {len(v)} numbers in a Vector({self.dim}) column")
        return "[" + ",".join(repr(x) for x in v) + "]"  # pgvector's text form, which is also JSON

    def process_result_value(self, value: Any, dialect: Dialect) -> list[float] | None:
        if value is None:
            return None
        if isinstance(value, str):
            return [float(x) for x in json.loads(value)]
        return [float(x) for x in cast("Iterable[Any]", value)]


def hnsw_index(name: str, column: str, *, m: int = 16, ef_construction: int = 64) -> sa.Index:
    """The index pgvector searches by cosine distance (a plain index on SQLite, which ignores the options)."""
    return sa.Index(
        name,
        column,
        postgresql_using="hnsw",
        postgresql_with={"m": m, "ef_construction": ef_construction},
        postgresql_ops={column: f"{SCHEMA}.vector_cosine_ops"},
    )


def cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """1 - cosine similarity, as pgvector's `<=>` gives it (a zero vector is at distance 1 from everything)."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return 1.0 if na == 0 or nb == 0 else 1.0 - dot / (na * nb)


def nearest(
    db: Session,
    column: InstrumentedAttribute[list[float]],
    query: Sequence[float],
    *,
    k: int = 10,
    where: Iterable[sa.ColumnElement[bool]] = (),
    select: sa.Select[Any] | None = None,
) -> list[tuple[Any, float]]:
    """The `k` rows closest to `query` by cosine distance, closest first, as (row, distance). `select` is a `select(Model)` with joins added. On Postgres the database orders
    (and uses an `hnsw_index`, filtered scans rescanned with pgvector 0.8's iterative scan); elsewhere every matching row is read and ordered in Python, fine for tests."""
    q = [float(x) for x in query]
    stmt = select if select is not None else sa.select(column.class_)
    for w in where:
        stmt = stmt.where(w)
    stmt = stmt.where(column.is_not(None))
    if db.get_bind().dialect.name == "postgresql":
        version = db.execute(sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar()
        try:
            if tuple(int(x) for x in str(version).split(".")[:2]) >= (0, 8):
                db.execute(sa.text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        except ValueError:
            pass
        literal = sa.cast(sa.bindparam(None, "[" + ",".join(repr(x) for x in q) + "]", type_=sa.Text), _PgVector(None))
        dist = cast("sa.ColumnElement[float]", column.op(f"OPERATOR({SCHEMA}.<=>)", return_type=sa.Float)(literal))
        rows = db.execute(stmt.add_columns(dist.label("distance")).order_by(dist).limit(k)).all()
        return [(r[0], float(r[-1])) for r in rows]
    out = [(r[0], cosine_distance(q, getattr(r[0], column.key))) for r in db.execute(stmt).all()]
    out.sort(key=lambda t: t[1])
    return out[:k]
