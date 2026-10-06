# Present at the repo root so pytest adds the root to sys.path, allowing
# `import app.*` from the tests package.

import pytest

from app import db


@pytest.fixture(autouse=True)
async def _isolated_db(tmp_path, monkeypatch):
    """Every test gets its own throwaway SQLite file, never the real leads.db."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_leads.db")
    await db.init_db()
    yield
