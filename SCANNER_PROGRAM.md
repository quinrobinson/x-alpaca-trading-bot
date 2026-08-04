# Scanner Program — Post-X Strategy Roadmap

> Adopted 2026-08-04 by owner decision. The X-signal-following strategy is
> **abandoned**: 45 closed trades at a 27% win rate and -$3,010, with entry
> snapshots showing every trade bought an already-extended move (mean RSI ~70,
> Bollinger-band position 0.81–0.95). The X pipeline code stays in the repo
> (parser/stream are reusable), but it stays disabled (`DISABLE_X_STREAM=true`)
> and no X API credits will be purchased.
>
> The bot's new core loop is the one proven by the failed-breakout scanner:
> **hypothesize → log-only observation → score with forward returns → promote
> only what survives.** This document is the authority for the program's
> phases and gates, in the same spirit as X_ALPACA_OPTIONS_HANDOFF.md was for
> Phases 1–11.

---

## Evidence base (why this direction)

Scored 2026-07-30 via `research/evaluate_scanner_events.py` over the first
132 live-logged failed-breakout events (2026-06-22 → 2026-07-30):

| Slice | n | +60m mean | down-hit @60m |
|---|---|---|---|
| All events | 132 | -0.25% | 50% |
| Volume ≥1.0x AND failure before 10:30 ET | 38 | **-0.60%** | 59% |
| Failure after 10:30 ET | 38 | +0.26% | 37% |

The edge is a 30–60 minute phenomenon that decays to nothing by the close —
so any execution carries a hard time exit, not a hold-to-close. The edge is
too thin for options friction (6–8% spreads + theta) but tradeable in shares.

---

## Phase S1 — Multi-hypothesis scanner lab (log-only, no new risk)

Generalize `scanners/` so several hypotheses run side by side against the
same `BarSource` / `record_event` plumbing, each writing to `scanner_events`
under its own `scanner_name` (schema already supports this).

Initial stable (all vendored pure-detection + unit tests, like failed_breakout):
1. `failed_breakout` — live today, now logging `volume_ratio`, 10:30 cutoff.
2. `vwap_reject` — morning strength that closes back below VWAP within a
   failure window (mean-reversion short).
3. `gap_fade` — gap-up ≥ threshold that breaks below the opening range low
   (trapped-gap-buyers short).
4. `prior_low_break` — breakdown through prior-day low (momentum
   continuation short; the control hypothesis — tests whether downside
   follow-through exists at all vs. only trap-reversion).

Scoring: `research/evaluate_scanner_events.py --scanner <name>` (already
parameterized). Weekly scoring cadence.

**Gate S1:** each hypothesis has ≥4 weeks of live log data and a scored
forward-return report. Promote any slice with mean ≤ -0.4% @60m and ≥55%
down-hit on n ≥ 25. Demote (stop scanning) anything flat or adverse.

## Phase S2 — Shares-based execution of the validated slice

Trade the already-validated failed-breakout slice in the underlying equity
(paper), NOT options — isolates signal quality from instrument friction.

Spec (owner-confirmed 2026-08-04; tunable via env):
- **Arming:** built now, deployed **disarmed** (`SCANNER_TRADING_ENABLED=false`).
  Armed only after ~4 weeks of fresh post-PR-#2 events (with live
  volume_ratio) confirm the slice out-of-sample — the original validation was
  retroactive on the same data that suggested the filter, so early arming
  would carry overfit risk.
- **Entry:** scanner event where `volume_ratio ≥ 1.0` and failure bar before
  10:30 ET → short the underlying at market.
- **Exit:** hard time exit 60 minutes after entry (market order). Protective
  buy-stop at +1.0% above entry. 15:55 ET failsafe flatten (positions should
  never live that long).
- **Sizing:** $1,000 notional per trade (owner-confirmed). Whole shares.
- **Risk:** max 3 concurrent scanner positions (owner-confirmed); scanner
  trades share the existing daily-loss kill switch; non-shortable/HTB
  rejections are logged and skipped, never retried.
- **Journal:** rows tagged so scanner trades never mix with legacy X trades
  in performance stats.

**Gate S2:** ≥30 fills. Realized mean return within 0.25% of the logged
signal expectation (slippage/borrow check), and the slice's live edge holds.

## Phase S3 — Defined-risk premium selling (after S1/S2)

The mirror of the failed X strategy: sell call spreads into the same
overextended states the X account used to buy (band position >0.9, RSI >70,
elevated IV). Captures theta + IV crush instead of fighting them.
Prerequisites: S1 produces a stable overextension signal; executor grows
multi-leg order support; new risk model (many small wins, occasional capped
loss) specced and unit-tested like strategy.py was.

**Not started until S2 gate passes and owner signs off on the spec.**

---

## Decommission notes (X strategy)

- `DISABLE_X_STREAM=true` stays set permanently on the droplet.
- Do not buy X API credits.
- Parser, stream wrapper, and their tests stay in the tree — they cost
  nothing and the fade-signal idea (X posts as a top-marker) remains a
  possible future S-series hypothesis if credits ever become free.
- Legacy `trades` rows (the 45 X trades) remain as the baseline dataset.
