from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from coordinator.app.main import Settings, create_app
from grid_helpers import ADMIN, USER


@pytest.fixture
def make_api(tmp_path):
    with ExitStack() as stack:
        def make(**overrides):
            settings = Settings(admin_key=ADMIN, user_key=USER,
                                db_path=str(tmp_path / "db.sqlite3"), **overrides)
            return stack.enter_context(TestClient(create_app(settings)))
        yield make


@pytest.fixture
def api(make_api):
    return make_api()


# Several app instances on one SQLite file behave like separate coordinator processes.
@pytest.fixture
def clients(tmp_path):
    path = str(tmp_path / "shared.sqlite3")
    with ExitStack() as stack:
        yield [stack.enter_context(TestClient(create_app(Settings(admin_key=ADMIN, user_key=USER, db_path=path))))
               for _ in range(6)]
