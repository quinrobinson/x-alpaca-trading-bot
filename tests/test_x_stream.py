"""Unit tests for x_alpaca_trading_bot.x_stream.

Doesn't touch the real X API — just exercises the callback wiring on the
XStreamListener subclass.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from x_alpaca_trading_bot.x_stream import XStreamListener


def _build_listener(**kwargs):
    # tweepy.StreamingClient.__init__ wants a bearer token but doesn't
    # actually call out, so we can pass a placeholder.
    return XStreamListener(
        bearer_token="test-token",
        target_account_id="12345",
        **kwargs,
    )


def test_on_connect_fires_optional_callback() -> None:
    fired: list[bool] = []
    listener = _build_listener(
        on_post=lambda *a, **kw: None,
        on_connect=lambda: fired.append(True),
    )
    listener.on_connect()
    assert fired == [True]


def test_on_connect_swallows_callback_exception() -> None:
    """A bad callback must not crash the stream thread."""
    def boom() -> None:
        raise RuntimeError("oops")

    listener = _build_listener(on_post=lambda *a, **kw: None, on_connect=boom)
    # Should not raise.
    listener.on_connect()


def test_on_connect_callback_is_optional() -> None:
    listener = _build_listener(on_post=lambda *a, **kw: None)
    # Should not raise even without a callback registered.
    listener.on_connect()


def test_on_keep_alive_fires_optional_callback() -> None:
    fired: list[bool] = []
    listener = _build_listener(
        on_post=lambda *a, **kw: None,
        on_keep_alive=lambda: fired.append(True),
    )
    listener.on_keep_alive()
    assert fired == [True]


def test_on_keep_alive_swallows_callback_exception() -> None:
    def boom() -> None:
        raise RuntimeError("oops")

    listener = _build_listener(on_post=lambda *a, **kw: None, on_keep_alive=boom)
    # Must not raise — the stream thread would die otherwise.
    listener.on_keep_alive()


def test_on_keep_alive_callback_is_optional() -> None:
    listener = _build_listener(on_post=lambda *a, **kw: None)
    listener.on_keep_alive()


def test_on_tweet_updates_last_received_and_calls_on_post() -> None:
    posts: list[tuple[str, str, datetime]] = []
    listener = _build_listener(
        on_post=lambda pid, text, posted: posts.append((pid, text, posted)),
    )
    assert listener.last_received_at is None

    fake_posted = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)

    class FakeTweet:
        id = 99
        text = "hello"
        created_at = fake_posted

    with patch("x_alpaca_trading_bot.x_stream.datetime") as dt_mod:
        # Pin "now" so the assertion is exact.
        pinned = datetime(2026, 5, 14, 14, 0, 5, tzinfo=timezone.utc)
        dt_mod.now.return_value = pinned
        # Real datetime constants still reachable for tweepy internals.
        dt_mod.side_effect = lambda *a, **kw: datetime(*a, **kw)
        listener.on_tweet(FakeTweet())

    assert posts == [("99", "hello", fake_posted)]
    assert listener.last_received_at is not None
