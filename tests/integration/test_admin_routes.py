"""The admin API over HTTP.

The single most important assertion here is the boring one: every route 401s
without a session. "Forgot to check auth on the new endpoint" is the classic way
an admin panel leaks, so the test enumerates the routes off the app itself rather
than a hand-written list — a route added later is covered the day it is added,
without anyone remembering to come back here.
"""

from __future__ import annotations

import os

import pytest

sa = pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")
pytest.importorskip("argon2")

from lake.metadata.models import Base  # noqa: E402

DSN = os.environ.get("LAKE_TEST_DB_DSN")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="set LAKE_TEST_DB_DSN to run admin route tests"),
]

EMAIL = "admin@example.com"
PASSWORD = "a-long-enough-password"


@pytest.fixture(scope="module")
def engine():
    eng = sa.create_engine(DSN)
    try:
        with eng.connect() as c:
            c.execute(sa.text("SELECT 1"))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"database unreachable: {exc}")
    yield eng
    eng.dispose()


@pytest.fixture
def client(engine, tmp_path, monkeypatch):
    """A TestClient over the real app, on a scratch database and a scratch registry."""
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    import lake.settings as settings_module
    from lake.metadata import session as session_module

    with engine.begin() as c:
        c.execute(sa.text("DROP VIEW IF EXISTS v_freshness"))
    Base.metadata.drop_all(engine)
    with engine.begin() as c:
        c.execute(sa.text("DROP TYPE IF EXISTS run_status"))
        c.execute(sa.text("DROP TYPE IF EXISTS schedule_kind"))
    Base.metadata.create_all(engine)

    # v_freshness belongs to the migration, not the ORM — /overview reads it.
    from pathlib import Path

    migration = Path(__file__).resolve().parents[2] / "migrations/versions/0001_initial_catalog.py"
    namespace: dict = {}
    exec(compile(migration.read_text(), str(migration), "exec"), namespace)
    with engine.begin() as c:
        c.execute(sa.text(namespace["V_FRESHNESS"]))

    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "sources.yaml").write_text(
        "sources:\n"
        "  - source_id: example\n"
        '    display_name: "An example"\n'
        "    kind: api\n"
        "    schedule: daily\n"
        "    module: lake.sources.gov_news.scraper:GovNewsScraper\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LAKE_SOURCES_CONFIG", str(configs / "sources.yaml"))
    monkeypatch.setenv("LAKE_NAS_ROOT", str(tmp_path / "nas"))
    monkeypatch.setenv("LAKE_LOG_DIR", str(tmp_path / "logs"))
    # Off, or a burst of login attempts in one test trips the limiter in the next.
    monkeypatch.setenv("LAKE_API_RATE_LIMIT_ENABLED", "false")
    settings_module.get_settings.cache_clear()

    scratch = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(session_module, "get_sessionmaker", lambda: scratch)

    from lake.api.admin import auth
    from lake.api.app import create_app

    auth.create_user(EMAIL, "Admin", PASSWORD)

    with TestClient(create_app()) as c:
        yield c

    settings_module.get_settings.cache_clear()


@pytest.fixture
def signed_in(client):
    r = client.post("/api/admin/login", json={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 200
    return client


#: The two routes an anonymous caller is *allowed* to reach, and why.
#:
#: `login` is the front door. `logout` deliberately takes no principal: it clears
#: whatever cookie was sent and says ok, so a stale or half-broken session can
#: always be thrown away. Requiring auth to log out is how someone gets stuck, and
#: it reveals nothing — there is no state to read and none to change.
PUBLIC = {"/api/admin/login", "/api/admin/logout"}


def _admin_routes(app) -> list[tuple[str, str]]:
    """Every admin route that must be behind the login, straight off the app.

    Enumerated rather than listed by hand, so a route added tomorrow is covered
    without anyone remembering to come back and add it here — which is exactly the
    kind of forgetting that leaks an admin panel.
    """
    out = []
    for path, methods in app.openapi()["paths"].items():
        if not path.startswith("/api/admin") or path in PUBLIC:
            continue
        for method in methods:
            out.append((method.upper(), path))
    return out


# --- the boundary ------------------------------------------------------------


def test_every_admin_route_refuses_an_anonymous_caller(client):
    """The one that matters. No cookie, no data — for every route, including the
    ones nobody has written yet."""
    routes = _admin_routes(client.app)
    assert routes, "no admin routes found — the enumeration is broken"

    for method, path in routes:
        # Fill in a plausible path param; the auth check runs before the handler.
        concrete = path.replace("{user_id}", "00000000-0000-0000-0000-000000000000").replace(
            "{name}", "whatever"
        )
        r = client.request(method, concrete, json={})
        assert r.status_code == 401, f"{method} {concrete} returned {r.status_code}, not 401"


def test_login_with_a_bad_password_is_401(client):
    r = client.post("/api/admin/login", json={"email": EMAIL, "password": "wrong"})
    assert r.status_code == 401


def test_login_sets_an_httponly_cookie(client):
    """httpOnly so no script can read it; SameSite so it does not ride along on a
    cross-site request."""
    r = client.post("/api/admin/login", json={"email": EMAIL, "password": PASSWORD})

    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_logout_ends_the_session(signed_in):
    assert signed_in.get("/api/admin/me").status_code == 200
    signed_in.post("/api/admin/logout")
    assert signed_in.get("/api/admin/me").status_code == 401


def test_logout_without_a_session_is_a_harmless_no_op(client):
    """The one non-login route an anonymous caller may reach, on purpose: a stale
    or half-broken session must always be throwable away. It reads no state and
    changes none, so answering 200 to a stranger gives them nothing."""
    assert client.post("/api/admin/logout").status_code == 200


# --- reads -------------------------------------------------------------------


def test_overview_answers_the_three_questions(signed_in):
    """What is stale, what failed, what went quiet."""
    body = signed_in.get("/api/admin/overview").json()

    assert set(body) == {"health", "freshness", "runs", "errors", "quiet"}
    assert set(body["health"]) >= {"sources", "stale", "runs_24h", "failures_24h"}


def test_settings_never_leaks_a_secret(signed_in):
    """A panel that can display an API key is a panel that can leak one."""
    monkeypatch_free = signed_in.get("/api/admin/settings").json()

    # Only the *_set booleans, never the value itself.
    assert monkeypatch_free["anthropic_api_key_set"] in (True, False)
    assert "anthropic_api_key" not in monkeypatch_free
    assert "db_dsn" not in monkeypatch_free


# --- the source registry -----------------------------------------------------


def test_sources_returns_the_file_verbatim(signed_in):
    body = signed_in.get("/api/admin/sources").json()
    assert "source_id: example" in body["content"]
    assert body["backups"] == []


def test_a_broken_edit_is_refused_and_changes_nothing(signed_in):
    before = signed_in.get("/api/admin/sources").json()["content"]

    r = signed_in.put("/api/admin/sources", json={"content": "sources: [garbage"})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"]

    assert signed_in.get("/api/admin/sources").json()["content"] == before


def test_a_good_edit_is_saved_backed_up_and_audited(signed_in):
    before = signed_in.get("/api/admin/sources").json()["content"]

    r = signed_in.put("/api/admin/sources", json={"content": before + "\n# edited\n"})
    assert r.status_code == 200
    assert r.json()["backup"]

    after = signed_in.get("/api/admin/sources").json()
    assert "# edited" in after["content"]
    assert len(after["backups"]) == 1

    # The audit entry carries the whole previous file — this is what stands in for
    # the git commit a browser edit does not produce.
    entry = signed_in.get("/api/admin/audit").json()[0]
    assert entry["action"] == "sources.update"
    assert entry["actor_email"] == EMAIL
    assert entry["detail"]["previous"] == before


def test_an_unchanged_save_is_a_no_op(signed_in):
    """Saving the file you were given must not fill the backup directory."""
    content = signed_in.get("/api/admin/sources").json()["content"]

    assert signed_in.put("/api/admin/sources", json={"content": content}).json()["unchanged"]
    assert signed_in.get("/api/admin/sources").json()["backups"] == []


def test_validate_does_not_write(signed_in):
    before = signed_in.get("/api/admin/sources").json()["content"]

    r = signed_in.post("/api/admin/sources/validate", json={"content": before + "\n# hm\n"})
    assert r.json()["ok"] is True

    assert signed_in.get("/api/admin/sources").json()["content"] == before


# --- users -------------------------------------------------------------------


def test_an_admin_can_create_another(signed_in):
    r = signed_in.post(
        "/api/admin/users",
        json={"email": "second@example.com", "display_name": "Second", "password": PASSWORD},
    )
    assert r.status_code == 200

    emails = [u["email"] for u in signed_in.get("/api/admin/users").json()]
    assert "second@example.com" in emails


def test_a_short_password_is_refused(signed_in):
    r = signed_in.post(
        "/api/admin/users",
        json={"email": "weak@example.com", "display_name": "W", "password": "short"},
    )
    assert r.status_code == 422


def test_you_cannot_disable_yourself(signed_in):
    """An admin who locks themselves out of the panel that unlocks them has to go
    find a shell. Refuse, rather than let them."""
    me = next(u for u in signed_in.get("/api/admin/users").json() if u["is_you"])

    r = signed_in.patch(f"/api/admin/users/{me['user_id']}", json={"is_active": False})
    assert r.status_code == 422
    assert "your own account" in r.json()["detail"]


def test_disabling_someone_revokes_their_sessions(signed_in, client):
    """Disabled must mean disabled *now*, not at their next login."""
    signed_in.post(
        "/api/admin/users",
        json={"email": "doomed@example.com", "display_name": "D", "password": PASSWORD},
    )

    # They sign in on their own browser...
    from fastapi.testclient import TestClient

    theirs = TestClient(signed_in.app)
    theirs.post("/api/admin/login", json={"email": "doomed@example.com", "password": PASSWORD})
    assert theirs.get("/api/admin/me").status_code == 200

    # ...and we disable them.
    doomed = next(
        u for u in signed_in.get("/api/admin/users").json() if u["email"] == "doomed@example.com"
    )
    assert (
        signed_in.patch(
            f"/api/admin/users/{doomed['user_id']}", json={"is_active": False}
        ).status_code
        == 200
    )

    # Their live session is dead, not just their next login.
    assert theirs.get("/api/admin/me").status_code == 401


def test_changing_your_password_needs_the_current_one(signed_in):
    """So a borrowed session cannot be turned into a permanent account takeover."""
    r = signed_in.post(
        "/api/admin/password",
        json={"current_password": "not-it", "new_password": "a-different-long-password"},
    )
    assert r.status_code == 422
