"""Short, stable ids for everything a reader can open.

A URL should address one thing, unambiguously, without a fight. Two earlier shapes
did not:

    seki_indicators:TABEL1_1~Uang beredar Luas (M2)
        `TABEL1_1` is the name of a spreadsheet file on Bank Indonesia's web
        server, and the series carries spaces and parentheses that a browser then
        percent-encodes into noise.

    seki_indicators:uang-beredar-dan-faktor-faktor-yang-mempengaruhinya~uang-beredar-luas-m2--2
        Readable, but ninety characters — and the `--2` is there because 139 of
        the 4,178 series have names that collide once slugified. A URL that has to
        carry a disambiguating suffix is a URL that has stopped being readable.

So the URL carries an id: `wm72qlsa`. Eight characters, one flat segment, nothing
to escape, and it means exactly one thing.

## It is derived, not stored

The id is a hash of the keys it points at — `(dataset_id, group_id, series)` — so a
rebuild of the replica produces the same ids for the same data, with no table to
keep in sync and nothing to migrate. Delete the replica, rebuild it, and every link
anyone ever shared still resolves.

That also means the id changes if the *thing* changes: if Bank Indonesia renames a
series, its id moves. That is the right trade. The alternative — a stored id, minted
once and kept forever — survives a rename but needs a table, a migration, and a
lifecycle, and it silently keeps pointing at a series whose meaning may have moved
underneath it.

## Why base32 and not hex

Its alphabet is `a-z2-7`: no `0` to confuse with `O`, no `1` to confuse with `l`. An
id can be read down a phone or typed from a screenshot. Hex would need 13 characters
for the same collision resistance and looks like a checksum; a UUID is 36.

## Collisions

Eight base32 characters is 40 bits. Across the 4,288 things the lake currently
publishes there are none, and the birthday bound says a collision becomes likely
around a million — two orders of magnitude beyond a lake that fits on one NUC. The
resolver still checks, and raises rather than serving the wrong series.
"""

from __future__ import annotations

import base64
import hashlib

#: How many characters of the digest to keep. 8 base32 chars = 40 bits.
LENGTH = 8

#: Separates the parts of the key being hashed. A unit separator, because it cannot
#: occur in a dataset id, a group id, or a series name — so `("a", "b|c")` and
#: `("a|b", "c")` cannot hash to the same id.
_SEP = "\x1f"


def make_id(dataset: str, group: str | None = None, series: str | None = None) -> str:
    """The id for one thing, from the keys that identify it.

    Deterministic: the same keys always give the same id, on any machine, after any
    rebuild. That is the whole point — the id is not a name assigned to a thing, it
    is a fingerprint of what the thing *is*.
    """
    key = _SEP.join([dataset, group or "", series or ""]).encode()
    digest = hashlib.blake2b(key, digest_size=16).digest()
    return base64.b32encode(digest).decode().rstrip("=").lower()[:LENGTH]
