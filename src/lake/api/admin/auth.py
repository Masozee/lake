"""Password hashing and session management for the admin panel.

Two rules hold this together, and both are about what an attacker gets when they
get something they should not have:

* The database never holds a password, and never holds a session token. It holds
  an Argon2id hash of the one and a SHA-256 of the other. A leaked dump — a stray
  backup, a `SELECT *` in a log — is not a way in.

* A session is a row, not a signed cookie. That is what makes revocation real:
  disable a user and their rows go, and they are logged out everywhere, at once.
  A signed cookie cannot be taken back before it expires.

Sessions are swept on read rather than by a timer. An expired row is treated as
absent whether or not anything has deleted it yet, so a sweeper that never runs
is a housekeeping problem and not a security one.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import delete, select

from lake.core.logging import get_logger
from lake.metadata.models import AuditLog, User, UserSession
from lake.metadata.session import session_scope

log = get_logger(__name__)

#: How long a login lasts. Long enough not to be a nuisance for an ops tool that
#: gets opened when something is broken; short enough that a forgotten laptop is
#: not a standing invitation.
SESSION_TTL = timedelta(days=7)

#: 32 bytes from the OS CSPRNG, urlsafe-encoded. Not a UUID: a UUID4 has 122 bits
#: and is designed to be unique, not to be unguessable.
TOKEN_BYTES = 32

#: OWASP's Argon2id baseline. Deliberately slow: this is the cost an attacker pays
#: per guess against a stolen hash.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=4)

#: Enforced on the way in. A length floor is the single most effective password
#: rule; complexity theatre mostly produces `Password1!`.
MIN_PASSWORD = 12

#: The cookie the browser carries. httpOnly so no script can read it, SameSite=Lax
#: so it does not ride along on a cross-site request.
COOKIE_NAME = "lake_admin"


class AuthError(Exception):
    """Login refused. Deliberately says nothing about why."""


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    return _hasher.hash(password)


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def _hash_token(token: str) -> str:
    """SHA-256, not Argon2. A 256-bit random token has nothing to brute-force, so
    the slow hash buys nothing here and would cost a lookup on every request."""
    return hashlib.sha256(token.encode()).hexdigest()


def check_password(user_id: uuid.UUID, password: str) -> bool:
    """Is this the user's current password? Opens no session.

    Separate from `authenticate` on purpose: re-confirming a password before a
    sensitive change must not mint a session as a side effect.
    """
    with session_scope() as s:
        user = s.get(User, user_id)
        if user is None or not user.is_active:
            return False
        return _verify(user.password_hash, password)


def create_user(email: str, display_name: str, password: str) -> uuid.UUID:
    """Create an admin. Raises ValueError if the email is taken.

    There is no web signup that reaches this without an existing admin's session:
    the first user comes from `lake admin create-user`, which needs shell access.
    """
    email = email.strip().lower()
    if not email or "@" not in email:
        raise ValueError("a real email address is required")

    user = User(
        user_id=uuid.uuid4(),
        email=email,
        display_name=display_name.strip() or email,
        password_hash=hash_password(password),
    )
    with session_scope() as s:
        if s.scalar(select(User).where(User.email == email)):
            raise ValueError(f"{email} already has an account")
        s.add(user)
    log.info("admin.user_created", email=email)
    return user.user_id


def authenticate(email: str, password: str, *, user_agent: str = "", ip: str = "") -> str:
    """Check a password and open a session. Returns the token for the cookie.

    Raises AuthError on any failure, with the same message every time: a login form
    that distinguishes "no such user" from "wrong password" is a way to enumerate
    who has an account.
    """
    email = email.strip().lower()

    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == email))

        # Verify even when there is no user, against a throwaway hash. Returning
        # early here would make a missing account measurably faster than a wrong
        # password, which is the same leak by a slower channel.
        if user is None:
            _verify(_DUMMY_HASH, password)
            raise AuthError("email or password is wrong")

        if not _verify(user.password_hash, password):
            log.warning("admin.login_failed", email=email, ip=ip)
            raise AuthError("email or password is wrong")

        if not user.is_active:
            log.warning("admin.login_disabled", email=email, ip=ip)
            raise AuthError("email or password is wrong")

        token = secrets.token_urlsafe(TOKEN_BYTES)
        s.add(
            UserSession(
                session_id=uuid.uuid4(),
                user_id=user.user_id,
                token_hash=_hash_token(token),
                expires_at=datetime.now(UTC) + SESSION_TTL,
                user_agent=user_agent[:500] or None,
                ip=ip or None,
            )
        )
        user.last_login_at = datetime.now(UTC)
        log.info("admin.login", email=email, ip=ip)

    return token


#: A real Argon2 hash of a value nobody knows, so the no-such-user path does the
#: same work as the wrong-password path. Computed once at import.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))


class Principal:
    """Who is making this request. Plain data — never holds the token."""

    __slots__ = ("display_name", "email", "user_id")

    def __init__(self, user_id: uuid.UUID, email: str, display_name: str) -> None:
        self.user_id = user_id
        self.email = email
        self.display_name = display_name


def resolve(token: str | None) -> Principal | None:
    """The user behind a cookie, or None. Expired and disabled resolve to None.

    Expiry is enforced here, on read — not by a sweeper. A cleanup job that stops
    running must never turn into a session that never ends.
    """
    if not token:
        return None

    with session_scope() as s:
        row = s.scalar(select(UserSession).where(UserSession.token_hash == _hash_token(token)))
        if row is None:
            return None
        if row.expires_at <= datetime.now(UTC):
            s.delete(row)  # take the chance to clean it up
            return None

        user = s.get(User, row.user_id)
        if user is None or not user.is_active:
            return None

        return Principal(user.user_id, user.email, user.display_name)


def logout(token: str | None) -> None:
    """Drop this one session. Other browsers stay signed in."""
    if not token:
        return
    with session_scope() as s:
        s.execute(delete(UserSession).where(UserSession.token_hash == _hash_token(token)))


def revoke_all(user_id: uuid.UUID) -> None:
    """Sign a user out everywhere. What makes 'disable' mean disabled."""
    with session_scope() as s:
        s.execute(delete(UserSession).where(UserSession.user_id == user_id))


def sweep_expired() -> int:
    """Delete expired session rows. Housekeeping only — `resolve` already refuses
    them, so this is about the size of the table, not about security."""
    with session_scope() as s:
        result = s.execute(delete(UserSession).where(UserSession.expires_at <= datetime.now(UTC)))
        # A DELETE always yields a CursorResult, which carries rowcount — but the
        # ORM's `execute` is typed as returning the general Result, which does not.
        return getattr(result, "rowcount", 0) or 0


def record(
    actor: Principal | None,
    action: str,
    *,
    target: str | None = None,
    detail: dict | None = None,
) -> None:
    """Write an audit entry. Called for every write the panel makes.

    `actor_email` is stored flat as well as by id, so deleting the user later does
    not erase who did the thing.
    """
    with session_scope() as s:
        s.add(
            AuditLog(
                entry_id=uuid.uuid4(),
                user_id=actor.user_id if actor else None,
                actor_email=actor.email if actor else "system",
                action=action,
                target=target,
                detail=detail or {},
            )
        )
