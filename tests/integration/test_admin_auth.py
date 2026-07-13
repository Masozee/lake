"""Admin authentication, against a real Postgres.

These are the most important tests in the project, because this is the only code
whose failure hands someone else the keys. Each one asserts a property an attacker
would try to break, not a happy path:

* a password is never stored, and a session token is never stored
* a wrong password and an unknown account are indistinguishable
* disabling a user logs them out everywhere, immediately
* an expired session is dead even if nothing has swept it

They skip when no database is reachable, so `pytest` still passes on a laptop.

    createdb lake_meta_test
    LAKE_TEST_DB_DSN=postgresql+psycopg://$USER@localhost/lake_meta_test pytest tests/integration
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

sa = pytest.importorskip("sqlalchemy")
pytest.importorskip("argon2")

from lake.metadata.models import AuditLog, Base, User, UserSession  # noqa: E402

DSN = os.environ.get("LAKE_TEST_DB_DSN")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="set LAKE_TEST_DB_DSN to run admin auth tests"),
]

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
def auth(engine, monkeypatch):
    """The auth module, bound to a freshly created scratch database."""
    from sqlalchemy.orm import sessionmaker

    from lake.api.admin import auth as module
    from lake.metadata import session as session_module

    with engine.begin() as c:
        c.execute(sa.text("DROP VIEW IF EXISTS v_freshness"))
    Base.metadata.drop_all(engine)
    with engine.begin() as c:
        c.execute(sa.text("DROP TYPE IF EXISTS run_status"))
        c.execute(sa.text("DROP TYPE IF EXISTS schedule_kind"))
    Base.metadata.create_all(engine)

    # session_scope() resolves get_sessionmaker() at call time, so this reaches
    # every function in the auth module without touching the module itself.
    scratch = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(session_module, "get_sessionmaker", lambda: scratch)

    return module


@pytest.fixture
def db(engine):
    """A session on the scratch database, for asserting on rows directly."""
    from sqlalchemy.orm import Session

    with Session(engine, expire_on_commit=False) as s:
        yield s


# --- what is stored ----------------------------------------------------------


def test_password_is_never_stored(auth, db):
    """The row holds an Argon2id hash. Not the password, not a reversible anything."""
    auth.create_user("ada@example.com", "Ada", PASSWORD)

    user = db.scalar(sa.select(User).where(User.email == "ada@example.com"))
    assert user is not None
    assert user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in user.password_hash


def test_session_token_is_never_stored(auth, db):
    """The database holds a SHA-256 of the token. A leaked dump is not a way in."""
    auth.create_user("tok@example.com", "Tok", PASSWORD)
    token = auth.authenticate("tok@example.com", PASSWORD)

    row = db.scalar(sa.select(UserSession))
    assert row is not None
    assert row.token_hash != token
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()


def test_uuid_keys_are_generated_in_python(auth):
    """The house convention — so no table needs a pgcrypto default."""
    assert isinstance(auth.create_user("uid@example.com", "Uid", PASSWORD), uuid.UUID)


# --- creating users ----------------------------------------------------------


def test_short_passwords_are_refused(auth):
    """A length floor is the single most effective password rule."""
    with pytest.raises(ValueError, match="at least"):
        auth.create_user("short@example.com", "Short", "tiny")


def test_email_must_be_unique(auth):
    auth.create_user("dupe@example.com", "One", PASSWORD)
    with pytest.raises(ValueError, match="already has an account"):
        auth.create_user("dupe@example.com", "Two", PASSWORD)


def test_email_is_normalised(auth, db):
    """`Ada@Example.COM` and `ada@example.com` must not be two accounts."""
    auth.create_user("  Ada@Example.COM ", "Ada", PASSWORD)
    assert db.scalar(sa.select(User).where(User.email == "ada@example.com")) is not None
    assert auth.authenticate("ADA@example.com", PASSWORD)  # and login normalises too


# --- logging in --------------------------------------------------------------


def test_wrong_password_and_unknown_user_are_indistinguishable(auth):
    """A login form that tells them apart is a way to enumerate who has an account."""
    auth.create_user("real@example.com", "Real", PASSWORD)

    with pytest.raises(auth.AuthError) as wrong:
        auth.authenticate("real@example.com", "not-the-password")
    with pytest.raises(auth.AuthError) as missing:
        auth.authenticate("ghost@example.com", "not-the-password")

    assert str(wrong.value) == str(missing.value)


def test_resolve_returns_the_user_behind_a_token(auth):
    auth.create_user("who@example.com", "Who", PASSWORD)
    who = auth.resolve(auth.authenticate("who@example.com", PASSWORD))

    assert who is not None
    assert who.email == "who@example.com"
    assert who.display_name == "Who"


@pytest.mark.parametrize("token", ["not-a-real-token", "", None])
def test_resolve_refuses_garbage(auth, token):
    assert auth.resolve(token) is None


def test_check_password_opens_no_session(auth, db):
    """Re-confirming a password before a sensitive change must not mint a session."""
    user_id = auth.create_user("chk@example.com", "Chk", PASSWORD)

    assert auth.check_password(user_id, PASSWORD) is True
    assert auth.check_password(user_id, "wrong") is False
    assert db.scalar(sa.select(UserSession)) is None


# --- ending a session --------------------------------------------------------


def test_an_expired_session_is_dead_even_if_nothing_swept_it(auth, engine):
    """Expiry is enforced on read. A sweeper that stops running must never become
    a session that never ends."""
    auth.create_user("exp@example.com", "Exp", PASSWORD)
    token = auth.authenticate("exp@example.com", PASSWORD)

    with engine.begin() as c:
        c.execute(sa.text("UPDATE user_sessions SET expires_at = now() - interval '1 second'"))

    assert auth.resolve(token) is None


def test_logout_drops_only_this_session(auth):
    """Signing out of one browser must not sign you out of the others."""
    auth.create_user("two@example.com", "Two", PASSWORD)
    laptop = auth.authenticate("two@example.com", PASSWORD)
    phone = auth.authenticate("two@example.com", PASSWORD)

    auth.logout(laptop)

    assert auth.resolve(laptop) is None
    assert auth.resolve(phone) is not None


def test_revoke_all_signs_a_user_out_everywhere(auth):
    """What makes 'disabled' mean disabled, rather than 'disabled at next login'."""
    user_id = auth.create_user("all@example.com", "All", PASSWORD)
    laptop = auth.authenticate("all@example.com", PASSWORD)
    phone = auth.authenticate("all@example.com", PASSWORD)

    auth.revoke_all(user_id)

    assert auth.resolve(laptop) is None
    assert auth.resolve(phone) is None


def test_disabling_a_user_locks_them_out(auth, engine):
    """Both halves: the session they hold dies, and they cannot get a new one."""
    auth.create_user("off@example.com", "Off", PASSWORD)
    token = auth.authenticate("off@example.com", PASSWORD)

    with engine.begin() as c:
        c.execute(sa.text("UPDATE users SET is_active = false WHERE email = 'off@example.com'"))

    assert auth.resolve(token) is None
    with pytest.raises(auth.AuthError):
        auth.authenticate("off@example.com", PASSWORD)


def test_a_session_for_a_deleted_user_resolves_to_nothing(auth, engine):
    auth.create_user("del@example.com", "Del", PASSWORD)
    token = auth.authenticate("del@example.com", PASSWORD)

    with engine.begin() as c:
        c.execute(sa.text("DELETE FROM users WHERE email = 'del@example.com'"))

    assert auth.resolve(token) is None


def test_sweep_deletes_only_expired_sessions(auth, engine):
    auth.create_user("sw@example.com", "Sw", PASSWORD)
    live = auth.authenticate("sw@example.com", PASSWORD)
    dead = auth.authenticate("sw@example.com", PASSWORD)

    with engine.begin() as c:
        c.execute(
            sa.text(
                "UPDATE user_sessions SET expires_at = now() - interval '1 hour' "
                "WHERE token_hash = :h"
            ),
            {"h": hashlib.sha256(dead.encode()).hexdigest()},
        )

    assert auth.sweep_expired() == 1
    assert auth.resolve(live) is not None
    assert auth.resolve(dead) is None


# --- the audit log -----------------------------------------------------------


def test_the_audit_log_outlives_the_user(auth, db, engine):
    """Deleting an account must never erase what it did."""
    auth.create_user("gone@example.com", "Gone", PASSWORD)
    who = auth.resolve(auth.authenticate("gone@example.com", PASSWORD))
    auth.record(who, "sources.update", target="configs/sources.yaml", detail={"previous": "x"})

    with engine.begin() as c:
        c.execute(sa.text("DELETE FROM users WHERE email = 'gone@example.com'"))

    entry = db.scalar(sa.select(AuditLog))
    assert entry is not None
    assert entry.user_id is None  # the FK was nulled...
    assert entry.actor_email == "gone@example.com"  # ...but we still know who
    assert entry.detail["previous"] == "x"


def test_expiry_is_seven_days(auth, db):
    """Long enough not to be a nuisance for a tool you open when things are broken;
    short enough that a forgotten laptop is not a standing invitation."""
    auth.create_user("ttl@example.com", "Ttl", PASSWORD)
    auth.authenticate("ttl@example.com", PASSWORD)

    row = db.scalar(sa.select(UserSession))
    assert row is not None
    assert timedelta(days=6) < row.expires_at - datetime.now(UTC) <= timedelta(days=7)
