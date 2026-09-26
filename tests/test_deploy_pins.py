"""The build that deploys must be the build that was tested.

Render installs dependencies fresh on every build. `requirements.txt` named
no versions and no Python version was pinned, so a deploy's environment was
whatever PyPI served that morning -- the deploy that worked and the deploy
that failed differed only by the day they ran.

What that cost: a rebuild picked up Python 3.14 and a newer SQLAlchemy whose
default DBAPI for a bare `postgresql://` URL is psycopg (v3) rather than
psycopg2. `psycopg2-binary` was installed and `psycopg` was not, so every
gunicorn worker died at import with
`ModuleNotFoundError: No module named 'psycopg'`. The diff being deployed
touched none of it. Render kept the previous version serving, so production
stayed up, but nothing could ship until this was found.

These tests are cheap and they pin the class of failure, not the instance.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def requirement_lines():
    for line in read("requirements.txt").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            yield line


def test_every_dependency_names_a_version():
    """An unpinned requirement is a deploy whose contents nobody chose."""
    unpinned = [line for line in requirement_lines() if "==" not in line]
    assert not unpinned, f"unpinned, so a rebuild can change them: {unpinned}"


def test_the_python_version_is_pinned():
    """Python 3.14 is what re-resolved every dependency and broke the build."""
    pin = read(".python-version").strip()
    assert re.fullmatch(r"\d+\.\d+(\.\d+)?", pin), f"unreadable pin: {pin!r}"
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert pin.startswith(running), (
        f".python-version is {pin} but the suite runs on {running}; "
        "production would be running something these tests never saw")


def test_the_database_driver_is_named_explicitly():
    """A bare postgresql:// lets SQLAlchemy choose, and its choice changed."""
    source = read("app.py")
    assert "postgresql+psycopg2://" in source, (
        "app.py must name the driver; a bare postgresql:// resolves to "
        "whichever DBAPI the installed SQLAlchemy defaults to")


def test_the_pinned_sqlalchemy_is_the_one_being_tested():
    """A pin that has drifted from what is installed pins nothing."""
    import sqlalchemy
    pinned = [line for line in requirement_lines() if line.lower().startswith("sqlalchemy==")]
    assert pinned, "SQLAlchemy is pinned directly, not left to a transitive resolve"
    assert pinned[0].split("==")[1] == sqlalchemy.__version__, (
        f"requirements pins {pinned[0]} but the suite is running "
        f"SQLAlchemy {sqlalchemy.__version__}")


def test_psycopg2_is_the_driver_that_url_actually_loads():
    """End to end: the scheme app.py builds resolves to the driver shipped."""
    from sqlalchemy.engine import make_url
    url = make_url("postgresql+psycopg2://user:pw@host/db")
    assert url.get_driver_name() == "psycopg2"
    assert url.get_dialect().import_dbapi().__name__ == "psycopg2"
