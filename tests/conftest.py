"""测试夹具：每个用例用独立的临时数据库。"""

from __future__ import annotations

import pytest

from app import config, db
from app.pricing import seed_builtin


@pytest.fixture()
def dbpath(tmp_path, monkeypatch):
    """把 config.DB_PATH 指到临时文件，避免污染真实库。"""
    path = tmp_path / "usage-test.db"
    monkeypatch.setattr(config, "DB_PATH", path, raising=False)
    db.init_db(path)
    with db.session(path) as conn:
        seed_builtin(conn)
    return path


@pytest.fixture()
def conn(dbpath):
    with db.session(dbpath) as connection:
        yield connection
