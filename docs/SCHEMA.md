# SCHEMA — what was resolved, and how

Spec §2.2 lists four things poe.ninja does not document, and §13 says: probe
and write down what you found, do not invent a plausible field name and move
on. This file records the state of each.

## Status of Phase 0

**Phase 0 has not been executed against the live API.** The machine this was
built on has no route to `poe.ninja` — outbound requests are refused by the
environment's network policy:

```
$ curl https://poe.ninja/poe1/api/economy/leagues
curl: (56) CONNECT tunnel failed, response 403
```

That is a fact about the build environment, not about the API. It has one
consequence you need to act on, and one you do not:

* **Act on this:** run `python scripts/probe.py --write-schema` once from
  your own machine. It captures a live payload per endpoint, prints the
  findings, and writes them into the block at the bottom of this file. Until
  then, the "Observed findings" section below is empty.
* **You do not need to act on this to trust the numbers.** The design choice
  described next means the tool does not depend on anyone having run the
  probe first.

## The design decision that follows from it

The obvious way to handle an unknown field convention is to pick the most
likely one, hardcode it, and note the assumption. For the `pay`/`receive`
direction that would have been the wrong call. Spec §2.2 is explicit that
getting it backwards "silently inverts every spread and produces
plausible-looking nonsense" — a hardcoded guess fails silently, and it fails
silently again if poe.ninja ever flips the convention.

So the direction is **not encoded anywhere in this codebase**. It is derived
from each payload, on every run, and the tool refuses to emit spreads when
the evidence is weak. `src/poeflip/direction.py` holds the whole of it.

### How the derivation works

`chaosEquivalent` is the anchor: poe.ninja *does* document it as the chaos
value of one unit of the line's currency. Everything else is measured
against it.

**Step 1 — denomination, resolved separately for each side.** A quoted rate
is either chaos-per-unit (tracking `chaosEquivalent`) or units-per-chaos
(tracking its reciprocal). For every line we compare the quote against both
hypotheses in log space, which makes the comparison scale-free, and take the
closer fit. Lines whose chaos value sits near 1.0 are skipped: for those, `x`
and `1/x` are too close to tell apart.

`pay` and `receive` are resolved **independently**, because a two-sided quote
may express each side in its own direction. Assuming they share a
denomination is exactly the mistake that inverts a spread.

**Step 2 — roles.** With both sides in the same unit, we count how many
lines put `pay` above `receive` and vice versa. Whichever side is
systematically dearer is the ask (what you buy at); the other is the bid.
This is counted, not assumed.

**Step 3 — refuse when unsure.** Fewer than 8 usable lines, or less than 85%
agreement on either step, raises `DirectionError` naming the endpoint. The
run stops instead of publishing spreads it cannot stand behind.

The same machinery resolves the exchange numeraire (§6.1). When
`core.primary` is not chaos, the conversion factor is fitted against the
stash feed's `chaosEquivalent` under both readings of `primaryValue`, the
tighter fit wins, and the result is cross-checked against `core.rates`. A
disagreement between the two readings is an error, not a shrug.

### What locks this down

`tests/test_direction.py` builds markets whose true prices are fixed by
construction — a Divine Orb worth 200c, bought at 205c and sold at 195c —
and asserts the resolver recovers them across **all four combinations** of
the two unknowns, for both possible role assignments. 21 cases in total.

The hand-verified reference case is `test_hand_verified_reference_case`,
worked through in full in its docstring. Two further tests assert the
resolver *refuses* rather than guesses: one starves it of lines, one gives it
a market with no consistent bid/ask ordering.

This means the answer the probe returns is a **confirmation**, not a
prerequisite. If poe.ninja's convention is the opposite of whatever you
expect, the tool already handles it.

### What still needs your eyes

Run the probe, then hand-check one currency in-game against the `CrossVenue`
sheet's `Stash Spread %`. The resolver guarantees internal consistency — that
the ask is the dearer side — but only you can confirm the feed describes the
market you are actually trading in.

## The other three unknowns

| Unknown | Status | Where |
|---|---|---|
| `pay` / `receive` sub-structure | Field name resolved by candidate list, failing loudly if none match | `schema.py`, `QUOTE_*_KEYS` |
| `sparkline` shapes | Not parsed at all — deliberately | see below |
| `SkillGem` `variant` vocabulary | Pattern with a documented assumption | `models/corrupt.py`, `VARIANT_RE` |

**`pay`/`receive` keys.** The rate is looked up under `value`, `rate`,
`ratio`, `primaryValue` in that order; counts under `count`,
`data_point_count`, and similar. If none is present, `SchemaError` names the
endpoint *and lists the keys that actually were there*, so the fix is
mechanical. The sample and listing counts are optional; the rate is not.

**Sparklines.** Nothing reads them. Spec §6.2 rules out using the sparkline
for trend — 7 points is too coarse and its time base is undocumented — and
trend comes from accumulated `snap_exchange` history instead. Rather than
parse a shape we would not use, the probe reports it and the code ignores it.
This is why the first day of running produces `NULL` trends: the history has
to accumulate first, and a fabricated number would be worse than a blank.

**Gem variants.** `VARIANT_RE` accepts `20`, `20/20`, `21/20`, `20/23`, with
an optional trailing `c`. That covers the forms the corrupt model needs to
construct (level and quality deltas off a 20/20 base). Variants it cannot
parse are skipped, not guessed at, and the probe prints every distinct
variant string it sees plus an explicit list of the ones this pattern misses.
If that list is non-empty and contains something you care about, widen the
pattern.

## Running the probe

```
python scripts/probe.py                 # fetch live, print findings
python scripts/probe.py --write-schema  # also write them in below
python scripts/probe.py --offline       # re-analyse data/probe/*.json
```

Then copy `data/probe/*.json` into `tests/fixtures/` if you want the real
payloads under test alongside the synthetic ones.

## Observed findings

<!-- PROBE-FINDINGS:BEGIN -->

_Empty — `scripts/probe.py` has not been run against the live API yet. See
"Status of Phase 0" above._

<!-- PROBE-FINDINGS:END -->
