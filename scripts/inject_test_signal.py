#!/usr/bin/env python3
"""Push a synthetic X post into a running bot via /debug/inject-post.

Lets you drive the full pipeline (parse → validate → risk → executor →
snapshot → trade) without depending on the real X stream. Useful when the
X Developer account is rate-limited or you want a deterministic test.

Configuration:
- Bot URL: --url, defaults to BOT_API_URL env or http://localhost:8000
- Auth token: --token, defaults to DEBUG_INJECT_TOKEN env

Usage:

    # The simplest invocation builds the post text from args
    python scripts/inject_test_signal.py SPY 500 2026-05-16 call 1.50

    # Or pass arbitrary post text
    python scripts/inject_test_signal.py --raw "$AAPL 6/20 $185c @ 2.50"

    # Against a remote bot:
    python scripts/inject_test_signal.py --url https://x-alpaca-bot.qr-project.dev \\
        SPY 500 2026-05-16 call 1.50
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

from dotenv import load_dotenv


def build_post_text(ticker: str, strike: str, expiration: str, opt_type: str, price: str) -> str:
    """Render a post in the format the bot's parser prompt expects."""
    short = "c" if opt_type == "call" else "p"
    return f"${ticker.upper()} {expiration} ${strike}{short} @ {price}"


def post(url: str, token: str, post_text: str, posted_at: datetime | None = None) -> dict:
    body = {"post_text": post_text}
    if posted_at:
        body["posted_at"] = posted_at.isoformat()
    data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        f"{url.rstrip('/')}/debug/inject-post",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"connection failed: {exc}", file=sys.stderr)
        sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(override=True)
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("BOT_API_URL", "http://localhost:8000"))
    parser.add_argument("--token", default=os.environ.get("DEBUG_INJECT_TOKEN", ""))
    parser.add_argument("--raw", help="Raw post text (overrides positional args)")
    parser.add_argument("--posted-at",
                        help="ISO timestamp for the post (defaults to now). Useful for testing time_age gate.")
    parser.add_argument("ticker", nargs="?", help="e.g. SPY")
    parser.add_argument("strike", nargs="?", help="e.g. 500")
    parser.add_argument("expiration", nargs="?", help="ISO date, e.g. 2026-05-16")
    parser.add_argument("opt_type", nargs="?", choices=["call", "put"])
    parser.add_argument("price", nargs="?", help="Posted entry price, e.g. 1.50")
    args = parser.parse_args(argv)

    if not args.token:
        print("error: DEBUG_INJECT_TOKEN not set (use --token or env)", file=sys.stderr)
        return 1

    if args.raw:
        post_text = args.raw
    else:
        missing = [n for n, v in zip(("ticker", "strike", "expiration", "opt_type", "price"),
                                     (args.ticker, args.strike, args.expiration, args.opt_type, args.price))
                   if not v]
        if missing:
            print(f"error: missing arg(s): {', '.join(missing)} (or use --raw)", file=sys.stderr)
            return 1
        post_text = build_post_text(args.ticker, args.strike, args.expiration, args.opt_type, args.price)

    posted_at = None
    if args.posted_at:
        posted_at = datetime.fromisoformat(args.posted_at.replace("Z", "+00:00"))
    else:
        posted_at = datetime.now(timezone.utc)

    print(f"POST {args.url}/debug/inject-post")
    print(f"  post_text: {post_text!r}")
    print(f"  posted_at: {posted_at.isoformat()}")
    result = post(args.url, args.token, post_text, posted_at)
    print(f"\nResult: {json.dumps(result, indent=2)}")

    print("\nWatch on droplet:  sudo journalctl -u x-alpaca-bot -f")
    print("Watch on dashboard: refresh and look for the signal in the feed within ~5s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
