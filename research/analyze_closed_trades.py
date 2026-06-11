#!/usr/bin/env python3
"""Mine closed-trade history for entry-condition patterns that distinguish
winners from losers.

What this does
--------------
1. Pulls every closed trade joined with its *entry* indicator_snapshot.
2. For each indicator dimension (IV, delta, RSI, VIX, options volume,
   spread%, time-of-day, DTE, etc.) splits the population into winners
   and losers and shows the distribution side-by-side.
3. Surfaces the indicators where winners and losers visibly diverge,
   ranked by separation strength.
4. Writes a markdown report under research/output/.

What this does NOT do
---------------------
- Claim statistical significance. With small N (typically <100 closed
  trades early on), every finding is a hypothesis, not a rule. Look for
  patterns strong enough to survive doubling N.
- Update the bot. Pure read-only against production tables — never
  writes, never imports from x_alpaca_trading_bot/.
- Apply any survivor-bias correction. Trades the validator REJECTED
  are excluded by design; this is "given a signal got through, what
  predicts outcome?" not "what would have predicted a winner among ALL
  signals."

Usage
-----
    export DATABASE_URL=postgresql://...
    python3 research/analyze_closed_trades.py [--out path/to/report.md]

Output
------
- Console summary of N, win rate, and the top diverging indicators.
- Markdown report with one section per indicator, including:
    * winners/losers count
    * mean, median, min, max per group
    * a textual histogram / bucket comparison
    * a "separation score" 0-1 (Cohen's d-style, magnitude only)
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any, Sequence

try:
    import psycopg
except ImportError:
    print("psycopg not installed. Run: pip install 'psycopg[binary]'", file=sys.stderr)
    sys.exit(1)


# ---- Indicators we mine ----------------------------------------------------

@dataclass(frozen=True)
class IndicatorSpec:
    """One thing we look at, with a label and the SQL expression that
    pulls it from the entry snapshot (or computed elsewhere)."""

    name: str
    label: str
    sql: str                 # SELECT-list expression
    interpret_high: str      # one-line: what a high value would suggest
    interpret_low: str       # one-line: what a low value would suggest


# The set is deliberately conservative — only fields we KNOW the bot
# captures (per indicator_snapshots schema). Greeks rank/percentile
# columns are excluded because they're None on the current plan.
INDICATORS: tuple[IndicatorSpec, ...] = (
    IndicatorSpec(
        name="delta",
        label="Option delta (moneyness)",
        sql="s.delta::float",
        interpret_high="deep ITM at entry",
        interpret_low="far OTM at entry",
    ),
    IndicatorSpec(
        name="iv",
        label="Implied volatility",
        sql="s.iv::float",
        interpret_high="rich premium / high expected move",
        interpret_low="cheap premium / quiet name",
    ),
    IndicatorSpec(
        name="rsi_14",
        label="Underlying RSI(14)",
        sql="s.rsi_14::float",
        interpret_high="overbought underlying",
        interpret_low="oversold underlying",
    ),
    IndicatorSpec(
        name="bb_position",
        label="Bollinger band position (0-1)",
        sql="s.bb_position::float",
        interpret_high="price at upper band",
        interpret_low="price at lower band",
    ),
    IndicatorSpec(
        name="vix",
        label="VIXY proxy",
        sql="s.vix::float",
        interpret_high="risk-off / nervy tape",
        interpret_low="risk-on / calm tape",
    ),
    IndicatorSpec(
        name="bid_ask_spread_pct",
        label="Option bid/ask spread %",
        sql="s.bid_ask_spread_pct::float",
        interpret_high="wide quote / poor liquidity / bad fill",
        interpret_low="tight quote / clean fill",
    ),
    IndicatorSpec(
        name="options_volume",
        label="Option contract volume",
        sql="s.options_volume::float",
        interpret_high="busy / lots of flow on this strike",
        interpret_low="thinly traded contract",
    ),
    IndicatorSpec(
        name="open_interest",
        label="Open interest",
        sql="s.open_interest::float",
        interpret_high="established contract",
        interpret_low="fresh / lightly populated contract",
    ),
    IndicatorSpec(
        name="entry_hour_et",
        label="Entry hour (ET, 0-23)",
        sql="EXTRACT(hour FROM (t.opened_at AT TIME ZONE 'America/New_York'))::float",
        interpret_high="late afternoon entry",
        interpret_low="opening-bell entry",
    ),
    IndicatorSpec(
        name="dte_days",
        label="Days to expiration at entry",
        sql="(t.expiration - (t.opened_at AT TIME ZONE 'America/New_York')::date)::float",
        interpret_high="more time / less theta urgency",
        interpret_low="0-2 DTE / fast theta bleed",
    ),
    IndicatorSpec(
        name="price_vs_vwap",
        label="Underlying % above/below VWAP",
        # underlying spot is not directly stored — approximate via option mid
        # vs delta is unreliable, so we use rsi as a proxy in this version.
        # Left here as a placeholder so the slot is reserved when we wire
        # in a real spot snapshot. Returning NULL means it's skipped.
        sql="NULL::float",
        interpret_high="extended from intraday mean",
        interpret_low="below intraday mean",
    ),
)


# ---- Data shaping ----------------------------------------------------------

@dataclass(frozen=True)
class TradeRow:
    trade_id: int
    ticker: str
    option_type: str
    pnl_pct: float
    exit_reason: str
    hold_minutes: int
    indicators: dict[str, float | None]


def fetch_trades(conn) -> list[TradeRow]:
    indicator_cols = ",\n".join(f"  {spec.sql} AS {spec.name}" for spec in INDICATORS)
    query = f"""
        SELECT
          t.id, t.ticker, t.option_type, t.pnl_pct::float, t.exit_reason,
          t.hold_minutes,
        {indicator_cols}
        FROM trades t
        LEFT JOIN LATERAL (
          SELECT *
          FROM indicator_snapshots
          WHERE signal_id = t.signal_id AND snapshot_type = 'entry'
          ORDER BY ts ASC
          LIMIT 1
        ) s ON true
        ORDER BY t.closed_at ASC
    """
    rows: list[TradeRow] = []
    with conn.cursor() as cur:
        cur.execute(query)
        for raw in cur.fetchall():
            (trade_id, ticker, opt_type, pnl_pct, exit_reason, hold_minutes,
             *indicator_values) = raw
            indicators = {
                spec.name: (float(v) if v is not None else None)
                for spec, v in zip(INDICATORS, indicator_values, strict=True)
            }
            rows.append(TradeRow(
                trade_id=trade_id,
                ticker=ticker,
                option_type=opt_type,
                pnl_pct=float(pnl_pct),
                exit_reason=exit_reason,
                hold_minutes=hold_minutes,
                indicators=indicators,
            ))
    return rows


# ---- Per-indicator analysis ------------------------------------------------

@dataclass
class IndicatorComparison:
    spec: IndicatorSpec
    winners: list[float]
    losers: list[float]

    def separation_score(self) -> float:
        """Cohen's-d-style magnitude. 0 = identical means; ~0.8 = large gap.

        Returns 0 when either group has <2 samples (can't form a stdev) or
        when both stdevs are zero. Capped at 3.0 to keep the report
        readable when a single outlier dominates a small group.
        """
        if len(self.winners) < 2 or len(self.losers) < 2:
            return 0.0
        w_sd = pstdev(self.winners)
        l_sd = pstdev(self.losers)
        pooled = ((w_sd ** 2 + l_sd ** 2) / 2) ** 0.5
        if pooled == 0:
            return 0.0
        d = abs(mean(self.winners) - mean(self.losers)) / pooled
        return min(d, 3.0)


def split_by_outcome(
    trades: Sequence[TradeRow], spec: IndicatorSpec
) -> IndicatorComparison:
    winners: list[float] = []
    losers: list[float] = []
    for t in trades:
        v = t.indicators.get(spec.name)
        if v is None:
            continue
        if t.pnl_pct > 0:
            winners.append(v)
        elif t.pnl_pct < 0:
            losers.append(v)
        # flat trades (pnl_pct == 0) excluded from both groups
    return IndicatorComparison(spec=spec, winners=winners, losers=losers)


# ---- Rendering -------------------------------------------------------------

def _fmt(x: float | None, places: int = 4) -> str:
    if x is None:
        return "—"
    return f"{x:,.{places}f}"


def render_indicator_section(cmp: IndicatorComparison) -> str:
    spec = cmp.spec
    if not cmp.winners and not cmp.losers:
        return f"### {spec.label}\n\n_No data — column was NULL on every entry snapshot._\n\n"

    def stats(xs: list[float]) -> dict[str, float | None]:
        if not xs:
            return {k: None for k in ("n", "mean", "median", "min", "max")}
        return {
            "n": float(len(xs)),
            "mean": mean(xs),
            "median": median(xs),
            "min": min(xs),
            "max": max(xs),
        }

    w = stats(cmp.winners)
    l = stats(cmp.losers)
    sep = cmp.separation_score()
    bar = "█" * int(sep * 10) + "░" * (10 - int(sep * 10))

    lines = [
        f"### {spec.label}",
        "",
        f"**Separation:** `{bar}` {sep:.2f} (Cohen's d magnitude — capped at 3.0)",
        "",
        "| group | n | mean | median | min | max |",
        "|---|---:|---:|---:|---:|---:|",
        f"| winners | {int(w['n'] or 0)} | {_fmt(w['mean'])} | {_fmt(w['median'])} | {_fmt(w['min'])} | {_fmt(w['max'])} |",
        f"| losers  | {int(l['n'] or 0)} | {_fmt(l['mean'])} | {_fmt(l['median'])} | {_fmt(l['min'])} | {_fmt(l['max'])} |",
        "",
    ]
    if sep > 0.5:
        if w["mean"] is not None and l["mean"] is not None:
            higher = "winners" if w["mean"] > l["mean"] else "losers"
            note = spec.interpret_high if higher == "winners" else spec.interpret_low
            lines.append(f"_Winners trend toward **{higher}-side** — {note}._")
            lines.append("")
    return "\n".join(lines)


def render_report(trades: list[TradeRow], comparisons: list[IndicatorComparison]) -> str:
    if not trades:
        return "# Closed-trade analysis\n\n_No closed trades found._\n"

    winners = sum(1 for t in trades if t.pnl_pct > 0)
    losers = sum(1 for t in trades if t.pnl_pct < 0)
    flats = len(trades) - winners - losers
    avg_pnl = mean(t.pnl_pct for t in trades)

    exit_reasons: dict[str, int] = {}
    for t in trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1
    exit_table = "\n".join(
        f"| {reason} | {count} |"
        for reason, count in sorted(exit_reasons.items(), key=lambda kv: -kv[1])
    )

    by_separation = sorted(comparisons, key=lambda c: c.separation_score(), reverse=True)
    top_three = by_separation[:3]

    head = [
        "# Closed-trade pattern analysis",
        "",
        f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}_",
        "",
        f"**N closed trades:** {len(trades)} ({winners}W / {losers}L / {flats} flat)",
        f"**Average P&L:** {avg_pnl:+.4f}",
        "",
        "> ⚠️ **Small-sample warning.** With this few closed trades, every",
        "> finding below is a hypothesis to watch, not a rule. Re-run after",
        "> N doubles and see if the same indicators stay on top.",
        "",
        "## Top diverging indicators",
        "",
        "These showed the largest gap between winner and loser populations.",
        "Skim these first.",
        "",
    ]
    for cmp in top_three:
        head.append(f"- **{cmp.spec.label}** — separation {cmp.separation_score():.2f}")
    head.append("")
    head.append("## Exit reason breakdown")
    head.append("")
    head.append("| exit_reason | count |")
    head.append("|---|---:|")
    head.append(exit_table)
    head.append("")
    head.append("## Per-indicator detail")
    head.append("")
    head.append("Sorted by separation (largest first).")
    head.append("")

    body = "\n".join(render_indicator_section(c) for c in by_separation)
    return "\n".join(head) + "\n" + body


# ---- Entry point ----------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "output" / "closed_trade_patterns.md",
        help="Markdown report path (default: research/output/closed_trade_patterns.md)",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres connection string (default: $DATABASE_URL)",
    )
    args = parser.parse_args(argv)

    if not args.database_url:
        print("DATABASE_URL not set (env or --database-url)", file=sys.stderr)
        return 2

    with psycopg.connect(args.database_url) as conn:
        trades = fetch_trades(conn)

    if not trades:
        print("No trades found.")
        return 0

    comparisons = [split_by_outcome(trades, spec) for spec in INDICATORS]
    report = render_report(trades, comparisons)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report)
    print(f"Wrote {args.out}")

    winners = sum(1 for t in trades if t.pnl_pct > 0)
    losers = sum(1 for t in trades if t.pnl_pct < 0)
    print(f"\nN={len(trades)}  winners={winners}  losers={losers}")
    print("\nTop diverging indicators:")
    by_separation = sorted(comparisons, key=lambda c: c.separation_score(), reverse=True)
    for cmp in by_separation[:5]:
        sep = cmp.separation_score()
        if sep == 0:
            continue
        wm = mean(cmp.winners) if cmp.winners else None
        lm = mean(cmp.losers) if cmp.losers else None
        print(
            f"  {cmp.spec.label:40s}  sep={sep:.2f}  "
            f"winner_mean={_fmt(wm)}  loser_mean={_fmt(lm)}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
