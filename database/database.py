"""PostgreSQL connection and session management for Restoka."""

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str | None = None, **engine_options: object) -> Engine:
    url = (
        database_url
        or os.environ.get("RESTOKA_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )

    if not url:
        raise RuntimeError(
            "No PostgreSQL URL provided. Set RESTOKA_DATABASE_URL or pass "
            "database_url='postgresql+psycopg://user:password@localhost:5432/restoka_eval'."
        )

    parsed_url = make_url(url)
    if parsed_url.get_backend_name() != "postgresql":
        raise ValueError(
            "Restoka supports only PostgreSQL. Set RESTOKA_DATABASE_URL to a "
            "postgresql+psycopg:// URL."
        )
    engine_options.setdefault("pool_pre_ping", True)
    return create_engine(parsed_url, **engine_options)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One atomic unit of work: commit on success, rollback on exception."""
    with factory() as session:
        with session.begin():
            yield session
