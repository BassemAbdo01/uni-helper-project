"""Point the suite at a throwaway database before app.py is ever imported.

Flask-SQLAlchemy binds its engines during init_app, so the database cannot be
swapped afterwards -- by the time a test module runs, `import app` has already
chosen where writes go. pytest loads conftest before test modules, which is the
only reliable place to make that choice.

Setting DATABASE_URL also puts the app in its production branch, so the suite
exercises the SECRET_KEY requirement and the hardened cookie settings rather
than the developer defaults. The one production setting that has to be undone
is SESSION_COOKIE_SECURE: the test client speaks http, so a Secure-only cookie
would never come back and every logged-in test would fail for the wrong reason.
"""

import os
import tempfile

_TEST_DB = os.path.join(tempfile.mkdtemp(prefix="nexora-tests-"), "test.db")

os.environ.setdefault("DATABASE_URL", "sqlite:///" + _TEST_DB.replace("\\", "/"))
os.environ.setdefault("SECRET_KEY", "test-only-secret-not-used-anywhere-real")

import pytest  # noqa: E402


@pytest.fixture()
def client():
    """A test client with a clean database and rate limiting switched off."""
    import app as nexora
    from app import db

    nexora.app.config.update(
        TESTING=True,
        SESSION_COOKIE_SECURE=False,   # test client is http-only
        WTF_CSRF_ENABLED=False,
    )
    nexora.limiter.enabled = False

    with nexora.app.app_context():
        db.drop_all()
        db.create_all()

    with nexora.app.test_client() as c:
        yield c

    with nexora.app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def rate_limited_client(client):
    """The same client with rate limiting left on, for the limiter's own tests."""
    import app as nexora
    nexora.limiter.enabled = True
    try:
        nexora.limiter.reset()
    except Exception:
        pass
    yield client
    nexora.limiter.enabled = False
