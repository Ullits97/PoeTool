# poe-flip

A screener and a logbook for Path of Exile 1 trading. Python does the
computation; Excel is a read-only frontend.

It answers two questions on a schedule:

1. What Currency Exchange orders should be standing right now, and at what rate?
2. Which gem corrupts are +EV *and* affordable at the current bankroll?

It is **not** a bot, a sniper, or a live price feed. poe.ninja data refreshes
roughly every 15 minutes and is HTTP-cached for about 5, so every row this
tool emits is a **candidate requiring manual verification in-game**.

## What it does not do

No calls to the official GGG trade API, no POESESSID, no scraping of live
listings, no automated trading or whispering, no poe.ninja builds/character
endpoints, no PoE 2. Deep links are emitted for you to click; the client is
never touched.

## Setup

```
pip install -r requirements.txt
```

Then **edit `config.yaml`** — the tool refuses to start until you do:

```yaml
app:
  user_agent: "poe-flip/0.1 (contact: you@yourdomain.tld)"
```

poe.ninja asks every client to identify itself with an app name and a real
contact address. That request is honoured here as a startup requirement, not
a suggestion.

Set your bankroll while you are in there:

```yaml
bankroll:
  total_chaos: 200          # everything gates off this
  max_position_pct: 0.25    # most capital in one currency order
  max_corrupt_fraction: 0.04  # most to risk on one corrupt attempt
```

## First run

```
python scripts/probe.py --write-schema   # once — see docs/SCHEMA.md
python run.py run
```

The probe confirms poe.ninja's current payload shapes and writes its findings
into `docs/SCHEMA.md`. It is not a prerequisite — the tool derives the field
conventions it needs from each payload — but running it is how you find out
if something upstream has changed.

## Commands

```
python run.py fetch      # fetch + persist only
python run.py analyse    # recompute from stored snapshots, no network
python run.py export     # write the xlsx from stored snapshots
python run.py run        # fetch -> analyse -> export (the default)
python run.py status     # league, last fetch per endpoint, counts, DB size
```

`--dry-run` logs the requests it would make without issuing them.
`--verbose` adds full request/response metadata. Logs go to stdout and
`data/run.log`.

`analyse` and `export` never touch the network, so they work offline.

## Scheduling

Hourly is a sensible cadence — poe.ninja refreshes every ~15 minutes, and the
client enforces a hard 5-minute floor between requests to any one endpoint
regardless of how often you invoke it.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
```

Run it from the project directory in an elevated PowerShell. It registers an
hourly Task Scheduler job named `poe-flip`. `-Unregister` removes it,
`-Hours 2` changes the interval. Check `data/run.log` to confirm it is firing.

## The two workbooks

Python owns `out/poe_data.xlsx` and **rewrites it completely on every run**.

You maintain a separate *cockpit* workbook that pulls from it via Power Query
and holds everything manual: open positions, notes, corrupt log entries.

This split is deliberate. If Python owned one combined workbook it would
destroy your input on every run.

**Consequence:** sheet names and column headers are a stable public
interface. If a build ever changes one, the run output says so explicitly
(`BREAKING CHANGE: sheet names or column headers differ from the previous
run`) and the `Meta` sheet repeats it, because your Power Query mappings will
need refreshing.

### Sheets

| Sheet | What's in it |
|---|---|
| `Meta` | Run timestamps, league, numeraire, fetch status per endpoint, row counts, resolved quote orientation |
| `Exchange_Orders` | The primary output: ranked standing-order recommendations |
| `CrossVenue` | Stash vs Exchange divergence — candidates only, with the caveat in a banner |
| `Corrupt_EV` | Gem corrupt EV, ranked, bankroll-gated |
| `Corrupt_Calibration` | Your logged outcomes vs the configured probabilities |
| `Filtered_Out` | Everything a gate excluded, with a readable reason |
| `History_Currency` | Long-format snapshot extract for the trailing window |

## Reading the output

**Trend and volatility are blank on your first runs.** They are computed from
accumulated snapshot history, not from the sparkline (7 points, undocumented
time base). A blank means "not enough history yet" — it is never a fabricated
zero. Volatility needs three snapshots inside the trend window; trend needs
two.

**`Exchange_Orders` is ranked by throughput, not margin.** The score is
`margin × capital deployable × expected fills per day`. Flipping at this
bankroll is limited by how often orders fill, not by how wide the margin is.
`Est Fills/Day` is a heuristic with stated assumptions — read the docstring
on `expected_fills_per_day` before leaning on it; it treats
`volumePrimaryValue` as a daily figure, which poe.ninja does not confirm, and
is best read as an upper bound.

**`CrossVenue` is a list of hypotheses.** The exchange figure is an observed
aggregate rate, not an executable order book. The stash figure is a listing
price, not a fill price. A divergence means "go look at this in-game", never
"this is free money". The workbook says so in a banner above the data.

**`Filtered_Out` is worth reading.** Every gate exclusion lands there with
its reason. If `Exchange_Orders` is emptier than you expect, that sheet tells
you which gate is biting.

## Corrupt logging

The corrupt module's real deliverable at a 200c bankroll is the dataset, not
the profit. Gem corrupting is a risk-of-ruin problem more than an EV problem:
the value sits in the tail (21/20, Vaal transforms) and a small bankroll can
be exhausted before the tail arrives. Hence the hard gate at 4% of bankroll
per attempt — at 200c that admits attempts up to ~8c, i.e. cheap gems only.
It widens on its own as the bankroll grows.

To start logging:

```
cp data/corrupt_log.example.csv data/corrupt_log.csv
```

Add a row per attempt. Python reads this file and never writes to it. Note
`vaal transform` in the `notes` column when a gem transforms, so calibration
can classify it. `Corrupt_Calibration` then shows observed frequency against
configured probability, with the sample size and a blunt note about how much
to read into it. Under 30 attempts, the answer is "nothing".

The outcome probabilities in `config.yaml` are community estimates, not
published figures — that is exactly why they live in config rather than in
code. Correct them from your own log as it grows.

## Expectations

Steady growth over weeks, constrained by order fill rate rather than market
depth. At a 200c bankroll a 10% margin returns 15-25c per trade, which does
not cover ten minutes of whispers and portals — which is why the primary
output is asynchronous Exchange orders that fill while you map, rather than
synchronous whisper-flipping. Low capital limits the absolute return, not the
efficiency.

## Development

```
python -m pytest
```

The suite covers chaos normalisation, the pay/receive spread direction in
every possible orientation, EV arithmetic against a hand-computed fixture,
bankroll gating boundaries, ETag/304 handling, the rate-limit floor, and
loud failure on renamed endpoints.

Layout: `ninja_client.py` has no domain logic and the model modules have no
HTTP, so a breaking upstream change is a one-file fix.

Deferred: crafting / base-flipping. poe.ninja carries no modifier weights, so
hit probability cannot be derived from it, and a single attempt can exceed
the whole current bankroll. See spec §9.
