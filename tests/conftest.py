"""Shared setup, before anything imports portal: a scratch DATA_DIR, a throwaway MongoDB database for this run
(gg_docs_test_<random>, dropped at the end; TEST_MONGODB_URL picks the server, default the local one), no worker,
GitHub sign-in "configured" with fake credentials (every GitHub call is stubbed in the tests)."""
import os
import tempfile
import uuid

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.update(DISABLE_WORKER="1", GITHUB_CLIENT_ID="cid", GITHUB_CLIENT_SECRET="csecret", TOKEN_ENCRYPTION_KEY="test",
                  MONGODB_URI=os.environ.get("TEST_MONGODB_URL", "mongodb://127.0.0.1:27017").rstrip("/")
                  + f"/gg_docs_test_{uuid.uuid4().hex[:10]}")

import pytest  # noqa: E402

from portal import db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def database():
    assert db.mongo.name.startswith("gg_docs_test_")  # never anything else
    db.init()
    yield
    db.client.drop_database(db.mongo.name)
