#!/usr/bin/env python3
"""Phase 0 — schema discovery.

Fetches one payload per endpoint, writes them to `data/probe/*.json`, and
prints the inferred structure of everything spec 2.2 leaves unspecified:

* the internal shape of `pay` / `receive` on stash currency lines
* **the direction convention of `pay` vs `receive`** — the highest-risk
  unknown in the build, resolved here against `chaosEquivalent` rather than
  assumed, with the evidence printed
* the shape of `sparkline` / `sparkLine` / `paySparkLine`
* the `variant` string vocabulary on SkillGem lines

Run it once against the live API, paste the "FINDINGS" block into
docs/SCHEMA.md, and copy the captured payloads into tests/fixtures/ so the
findings are locked behind tests.

    python scripts/probe.py                 # fetch live, then analyse
    python scripts/probe.py --offline       # re-analyse data/probe/*.json
    python scripts/probe.py --write-schema  # also update docs/SCHEMA.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from poeflip.config import load_config  # noqa: E402
from poeflip.direction import QuoteSample, resolve_orientation  # noqa: E402
from poeflip.errors import PoeFlipError  # noqa: E402
from poeflip.ninja_client import NinjaClient  # noqa: E402
from poeflip.schema import parse_leagues  # noqa: E402
from poeflip.store import Store  # noqa: E402

PROBE_DIR = ROOT / "data" / "probe"
SCHEMA_DOC = ROOT / "docs" / "SCHEMA.md"

MARKER_BEGIN = "<!-- PROBE-FINDINGS:BEGIN -->"
MARKER_END = "<!-- PROBE-FINDINGS:END -->"


def describe(value: Any, depth: int = 0) -> str:
    """One-line structural description of an arbitrary JSON value."""
    if isinstance(value, dict):
        if depth >= 2:
            return f"object({len(value)} keys)"
        inner = ", ".join(f"{k}: {describe(v, depth + 1)}" for k, v in list(value.items())[:12])
        return "{" + inner + ("}" if len(value) <= 12 else ", ...}")
    if isinstance(value, list):
        if not value:
            return "array(empty)"
        return f"array[{len(value)}] of {describe(value[0], depth + 1)}"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if value is None:
        return "null"
    return "string"


def key_profile(blocks: list[dict[str, Any]]) -> list[str]:
    """Which keys appear on a repeated sub-object, how often, and of what type."""
    counts: Counter[str] = Counter()
    types: dict[str, Counter[str]] = {}
    for block in blocks:
        for key, value in block.items():
            counts[key] += 1
            types.setdefault(key, Counter())[describe(value)] += 1
    total = len(blocks) or 1
    return [
        f"  {key:<24} present in {counts[key]:>4}/{total}  type(s): "
        + ", ".join(f"{t} x{n}" for t, n in types[key].most_common(3))
        for key in sorted(counts, key=lambda k: -counts[k])
    ]


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------
def capture(offline: bool, config_path: str) -> dict[str, Any]:
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    payloads: dict[str, Any] = {}

    if offline:
        for path in sorted(PROBE_DIR.glob("*.json")):
            payloads[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        if not payloads:
            raise PoeFlipError(
                f"--offline was given but {PROBE_DIR} holds no captured payloads. "
                "Run `python scripts/probe.py` once with network access first."
            )
        return payloads

    cfg = load_config(config_path)
    with Store(cfg.db_path) as store, NinjaClient(
        user_agent=cfg.app.user_agent,
        cache=store,
        min_fetch_interval_minutes=cfg.app.min_fetch_interval_minutes,
    ) as client:
        leagues_result = client.leagues()
        payloads["leagues"] = leagues_result.payload
        leagues = parse_leagues(leagues_result.payload, endpoint=leagues_result.endpoint)
        league = cfg.league.pinned_id if cfg.league.mode == "pinned" else leagues[0].id
        print(f"league: {league}")

        payloads["stash_currency_Currency"] = client.stash_currency(league, "Currency").payload
        payloads["stash_currency_Fragment"] = client.stash_currency(league, "Fragment").payload
        payloads["exchange_Currency"] = client.exchange(league, "Currency").payload
        payloads["item_SkillGem"] = client.stash_items(league, "SkillGem").payload

    for name, payload in payloads.items():
        if payload is not None:
            (PROBE_DIR / f"{name}.json").write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
    print(f"captured {len(payloads)} payload(s) to {PROBE_DIR}")
    return payloads


# --------------------------------------------------------------------------
# analysis of the unknowns
# --------------------------------------------------------------------------
def analyse(payloads: dict[str, Any]) -> str:
    out: list[str] = []

    def emit(text: str = "") -> None:
        out.append(text)

    emit(f"Probed at {datetime.now(timezone.utc).replace(microsecond=0).isoformat()}")
    emit()

    stash = payloads.get("stash_currency_Currency")
    if not isinstance(stash, dict) or not isinstance(stash.get("lines"), list):
        emit("## stash currency: NOT AVAILABLE — cannot resolve pay/receive")
        return "\n".join(out)

    lines = [ln for ln in stash["lines"] if isinstance(ln, dict)]
    emit(f"## 1. Stash currency lines ({len(lines)} lines)")
    emit()
    emit("Top-level line keys:")
    emit("\n".join(key_profile(lines)))
    emit()

    # -- pay / receive sub-structure --------------------------------------
    for side in ("pay", "receive"):
        blocks = [ln[side] for ln in lines if isinstance(ln.get(side), dict)]
        emit(f"### `{side}` sub-structure ({len(blocks)}/{len(lines)} lines carry it)")
        if blocks:
            emit("\n".join(key_profile(blocks)))
            emit()
            emit(f"Example `{side}` block:")
            emit("  " + json.dumps(blocks[0], indent=2).replace("\n", "\n  "))
        else:
            emit(f"  no line carries a `{side}` object")
        emit()

    # -- direction, the high-risk unknown ---------------------------------
    emit("## 2. pay/receive DIRECTION — the high-risk unknown (spec 2.2)")
    emit()
    emit("Method: `chaosEquivalent` is documented as the chaos value of one unit of")
    emit("the line's currency, so it serves as the reference. For each quoted side we")
    emit("test, in log space across every line, whether the quoted value tracks")
    emit("`chaosEquivalent` (chaos per unit) or its reciprocal (units per chaos), and")
    emit("then count which side is systematically the more expensive one. Nothing is")
    emit("assumed; a weak majority raises an error instead of returning a guess.")
    emit()

    samples = []
    for ln in lines:
        chaos = ln.get("chaosEquivalent")
        if not isinstance(chaos, (int, float)):
            continue
        samples.append(
            QuoteSample(
                name=str(ln.get("currencyTypeName") or ln.get("detailsId") or "?"),
                chaos_equivalent=float(chaos),
                pay_value=_first_number(ln.get("pay")),
                receive_value=_first_number(ln.get("receive")),
            )
        )

    try:
        orientation = resolve_orientation(samples, endpoint="probe:stash_currency")
    except PoeFlipError as exc:
        emit("RESOLUTION FAILED:")
        emit(f"  {exc}")
    else:
        emit("RESOLVED:")
        emit("  " + orientation.describe().replace("\n", "\n  "))
        emit()
        emit("Worked examples (denomination fit):")
        for example in orientation.pay.examples:
            emit(f"  pay      {example}")
        for example in orientation.receive.examples:
            emit(f"  receive  {example}")
        emit()
        emit("Consequence for the code:")
        emit(f"  - you BUY at `{orientation.ask_field}` and SELL at `{orientation.bid_field}`")
        emit("  - spread = (ask - bid) / bid, computed by Orientation.spread_pct")
        emit()
        emit("Hand-check this against one currency in-game before trusting any spread.")
    emit()

    # -- sparkline shapes --------------------------------------------------
    emit("## 3. Sparkline shapes")
    emit()
    seen: set[str] = set()
    for source_name, payload in payloads.items():
        if not isinstance(payload, dict):
            continue
        for line in payload.get("lines", [])[:50]:
            if not isinstance(line, dict):
                continue
            for key, value in line.items():
                if "spark" in key.lower() and key not in seen:
                    seen.add(key)
                    emit(f"  {source_name}.{key}: {describe(value)}")
    if not seen:
        emit("  no sparkline-ish key found on any captured line")
    emit()

    # -- gem variant vocabulary -------------------------------------------
    emit("## 4. SkillGem `variant` vocabulary")
    emit()
    gems = payloads.get("item_SkillGem")
    if isinstance(gems, dict) and isinstance(gems.get("lines"), list):
        variants: Counter[str] = Counter()
        corrupted_variants: Counter[str] = Counter()
        for line in gems["lines"]:
            if not isinstance(line, dict):
                continue
            variant = line.get("variant")
            variants[str(variant)] += 1
            if line.get("corrupted"):
                corrupted_variants[str(variant)] += 1
        emit(f"  {len(variants)} distinct variant string(s) across {len(gems['lines'])} lines")
        emit("  most common:")
        for variant, count in variants.most_common(25):
            emit(f"    {variant!r:<18} x{count:<6} (corrupted: {corrupted_variants.get(variant, 0)})")
        unparsed = [v for v in variants if not _parses(v)]
        if unparsed:
            emit()
            emit("  NOT parsed by corrupt.VARIANT_RE — update the pattern if these matter:")
            for variant in sorted(unparsed)[:25]:
                emit(f"    {variant!r}")
    else:
        emit("  SkillGem payload not available")
    emit()

    # -- exchange core -----------------------------------------------------
    emit("## 5. Exchange `core` (numeraire)")
    emit()
    exchange = payloads.get("exchange_Currency")
    if isinstance(exchange, dict) and isinstance(exchange.get("core"), dict):
        core = exchange["core"]
        emit(f"  core.primary   = {core.get('primary')!r}")
        emit(f"  core.secondary = {core.get('secondary')!r}")
        emit(f"  core.rates     = {describe(core.get('rates'))}")
        emit(f"  core.items     = {describe(core.get('items'))}")
        exchange_lines = [ln for ln in exchange.get("lines", []) if isinstance(ln, dict)]
        if exchange_lines:
            emit()
            emit("  Exchange line keys:")
            emit("\n".join(key_profile(exchange_lines)))
    else:
        emit("  exchange payload not available")

    return "\n".join(out)


def _first_number(block: Any) -> float | None:
    """Pull the rate out of a pay/receive block without assuming its key name."""
    if not isinstance(block, dict):
        return None
    from poeflip.schema import QUOTE_VALUE_KEYS

    for key in QUOTE_VALUE_KEYS:
        value = block.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _parses(variant: str) -> bool:
    from poeflip.models.corrupt import parse_variant

    return parse_variant(variant) is not None


def write_schema_doc(findings: str) -> None:
    if not SCHEMA_DOC.exists():
        raise PoeFlipError(f"{SCHEMA_DOC} does not exist; create it first")
    text = SCHEMA_DOC.read_text(encoding="utf-8")
    if MARKER_BEGIN not in text or MARKER_END not in text:
        raise PoeFlipError(
            f"{SCHEMA_DOC} has no {MARKER_BEGIN} / {MARKER_END} block to write into"
        )
    head, _, rest = text.partition(MARKER_BEGIN)
    _, _, tail = rest.partition(MARKER_END)
    block = f"{MARKER_BEGIN}\n\n```\n{findings}\n```\n\n{MARKER_END}"
    SCHEMA_DOC.write_text(head + block + tail, encoding="utf-8")
    print(f"\nwrote findings into {SCHEMA_DOC}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="poe.ninja schema discovery (Phase 0)")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument(
        "--offline", action="store_true", help="re-analyse previously captured payloads"
    )
    parser.add_argument(
        "--write-schema", action="store_true", help="write findings into docs/SCHEMA.md"
    )
    args = parser.parse_args(argv)

    try:
        payloads = capture(args.offline, args.config)
        findings = analyse(payloads)
    except PoeFlipError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print()
    print("=" * 78)
    print("FINDINGS")
    print("=" * 78)
    print(findings)

    if args.write_schema:
        write_schema_doc(findings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
