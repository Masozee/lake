"""Turning an exception into something a page can show a person."""

from __future__ import annotations


def message(exc: Exception) -> str:
    """The message inside an exception, without Python's punctuation around it.

    `str(KeyError("no dataset 'zzzzzzzz'"))` is `"no dataset 'zzzzzzzz'"` — quotes and
    all, because KeyError reprs its argument. Stripping quote characters off the ends
    eats the closing one and leaves `no dataset 'zzzzzzzz`, which reads like the
    server truncated something. The argument itself is already the message.
    """
    if exc.args and isinstance(exc.args[0], str):
        return exc.args[0]
    return str(exc)
