"""Shared fixtures: an in-memory DB and a tmp-dir storage backend.

The ``storage`` fixture installs a LocalStorage rooted at a fresh tmp dir as the
process-wide backend — that tmp dir *is* the "bucket" the reconciliation tests
check the DB against, so no AWS is involved.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 — registers every model on Base before create_all
from app.db.session import Base
from app.services.storage import LocalStorage, set_storage


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def storage(tmp_path):
    backend = LocalStorage(tmp_path)
    set_storage(backend)
    try:
        yield backend
    finally:
        set_storage(None)  # reset the singleton so other tests rebuild from settings
