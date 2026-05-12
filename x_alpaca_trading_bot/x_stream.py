"""Phase 2 — X API v2 filtered stream listener for a single target account.

Wraps tweepy.StreamingClient. On each tweet from the target author the
provided `on_post` callback fires with (post_id, post_text, posted_at).

Tweepy handles low-level reconnection; this module tracks `last_received_at`
so the orchestrator can trip the connection kill switch if no posts arrive for
longer than the configured stall window.

This module is pure plumbing — it does not parse, validate, or journal. The
orchestrator wires on_post to the parser and journal in Phase 7.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import tweepy

logger = logging.getLogger(__name__)

# Type alias — keeps call sites readable.
OnPost = Callable[[str, str, datetime], None]

# Matches "from:<numeric_id>" — the only rule we care about right now.
_RULE_TAG = "target_account"


class XStreamListener(tweepy.StreamingClient):
    """Internal subclass: forwards tweet events to a user-provided callback."""

    def __init__(
        self,
        bearer_token: str,
        target_account_id: str,
        on_post: OnPost,
    ) -> None:
        super().__init__(bearer_token=bearer_token, wait_on_rate_limit=True)
        self._target_account_id = target_account_id
        self._on_post = on_post
        self._last_received_at: datetime | None = None
        self._last_received_lock = threading.Lock()

    # ---- Public ----

    @property
    def last_received_at(self) -> datetime | None:
        """Timestamp of the most recent tweet processed (UTC)."""
        with self._last_received_lock:
            return self._last_received_at

    def configure_rules(self) -> None:
        """Reset filter rules to exactly one rule: from:<target_account_id>.

        Idempotent — deletes any existing rules first so re-runs don't duplicate.
        """
        existing = self.get_rules()
        # `existing.data` is None when no rules exist
        existing_ids = [r.id for r in (existing.data or [])]
        if existing_ids:
            self.delete_rules(existing_ids)
            logger.info("Deleted %d existing stream rules", len(existing_ids))

        rule = tweepy.StreamRule(
            value=f"from:{self._target_account_id}",
            tag=_RULE_TAG,
        )
        self.add_rules(rule)
        logger.info("Added stream rule: from:%s", self._target_account_id)

    # ---- Tweepy overrides ----

    def on_tweet(self, tweet: tweepy.Tweet) -> None:  # type: ignore[override]
        # Tweet `created_at` is timezone-aware when expansions request it,
        # otherwise it's missing; fall back to wall-clock UTC.
        posted_at = getattr(tweet, "created_at", None) or datetime.now(timezone.utc)
        with self._last_received_lock:
            self._last_received_at = datetime.now(timezone.utc)
        try:
            self._on_post(str(tweet.id), tweet.text or "", posted_at)
        except Exception:  # noqa: BLE001 — never let a callback kill the stream
            logger.exception("on_post callback raised; continuing stream")

    def on_connect(self) -> None:  # type: ignore[override]
        logger.info("X stream connected")

    def on_disconnect(self) -> None:  # type: ignore[override]
        logger.warning("X stream disconnected")

    def on_errors(self, errors: list[dict[str, Any]]) -> None:  # type: ignore[override]
        for err in errors:
            logger.error("X stream error: %s", err)

    def on_request_error(self, status_code: int) -> None:  # type: ignore[override]
        logger.error("X stream HTTP error %s", status_code)


def make_listener(
    bearer_token: str,
    target_account_id: str,
    on_post: OnPost,
) -> XStreamListener:
    """Factory: build a listener and configure its filter rule.

    The returned listener must have `.filter(...)` called by the orchestrator
    to actually start streaming. Pulled apart so callers can choose to call
    `.filter(threaded=True)` for a background thread or `.filter()` to block.
    """
    listener = XStreamListener(bearer_token, target_account_id, on_post)
    listener.configure_rules()
    return listener
