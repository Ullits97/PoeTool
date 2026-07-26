"""Resolve the pay/receive convention empirically, every run.

Spec 2.2 calls the direction of `pay` vs `receive` "the single highest-risk
unknown in the whole build: getting it backwards silently inverts every
spread and produces plausible-looking nonsense".

So this module does not encode a convention. It *derives* one from the
payload, using `chaosEquivalent` — a figure whose meaning poe.ninja does
document (the chaos value of one unit of the line's currency) — as the
anchor, and refuses to return an answer when the evidence is weak.

Two unknowns are resolved independently:

1. **Denomination**, per field. A quoted rate can be either
   ``chaos per 1 unit`` (DIRECT, i.e. value ~= chaosEquivalent) or
   ``units per 1 chaos`` (RECIPROCAL, i.e. value ~= 1 / chaosEquivalent).
   These are inferred separately for `pay` and for `receive`, because a
   two-sided quote may well express each side in its own direction — that
   is exactly the trap that inverts a spread.

2. **Roles.** Once both sides are expressed in the same unit, whichever
   side is systematically the more expensive one is the side you buy at
   (the ask); the other is the side you sell into (the bid). This is
   measured by counting lines, not assumed.

The result carries its own evidence (sample sizes, agreement fractions,
worst-case residuals) so `scripts/probe.py` can print it into docs/SCHEMA.md
and tests can assert against a hand-checked reference case.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Iterable, Literal

from .errors import DirectionError

Denomination = Literal["chaos_per_unit", "units_per_chaos"]

# A line only discriminates between the two denominations when its chaos
# value is far enough from 1.0 that x and 1/x are meaningfully different.
MIN_DISCRIMINATING_RATIO = 1.5

# Below this many usable lines we do not trust the inference at all.
MIN_USABLE_LINES = 8

# Fraction of usable lines that must agree before we accept a conclusion.
MIN_AGREEMENT = 0.85


@dataclass(frozen=True)
class FieldDenomination:
    """How one quoted side is scaled, plus the evidence for that call."""

    field_name: str
    denomination: Denomination
    usable_lines: int
    agreement: float
    median_rel_error: float
    examples: tuple[str, ...] = ()

    def to_chaos_per_unit(self, value: float) -> float:
        """Express a raw quoted value as chaos per one unit of the currency."""
        if self.denomination == "chaos_per_unit":
            return value
        if value == 0:
            raise ZeroDivisionError("cannot invert a zero rate")
        return 1.0 / value


@dataclass(frozen=True)
class Orientation:
    """The fully resolved pay/receive convention for one payload."""

    pay: FieldDenomination
    receive: FieldDenomination
    # Which named field is the more expensive side once both are in chaos/unit.
    ask_field: str  # you BUY at this price
    bid_field: str  # you SELL at this price
    role_sample: int
    role_agreement: float
    notes: tuple[str, ...] = field(default_factory=tuple)

    def chaos_per_unit(self, field_name: str, value: float) -> float:
        if field_name == "pay":
            return self.pay.to_chaos_per_unit(value)
        if field_name == "receive":
            return self.receive.to_chaos_per_unit(value)
        raise KeyError(f"unknown quote field {field_name!r}")

    def spread_pct(self, pay_value: float, receive_value: float) -> float | None:
        """Round-trip cost of crossing this currency's stash market, as a fraction.

        Positive means the ask sits above the bid, which is the normal state
        of any market. Returns None when either side is missing or non-positive.
        """
        if not pay_value or not receive_value or pay_value <= 0 or receive_value <= 0:
            return None
        values = {"pay": pay_value, "receive": receive_value}
        ask = self.chaos_per_unit(self.ask_field, values[self.ask_field])
        bid = self.chaos_per_unit(self.bid_field, values[self.bid_field])
        if bid <= 0:
            return None
        return (ask - bid) / bid

    def describe(self) -> str:
        lines = [
            f"pay.value       -> {self.pay.denomination} "
            f"(n={self.pay.usable_lines}, agreement={self.pay.agreement:.1%}, "
            f"median rel.err={self.pay.median_rel_error:.3%})",
            f"receive.value   -> {self.receive.denomination} "
            f"(n={self.receive.usable_lines}, agreement={self.receive.agreement:.1%}, "
            f"median rel.err={self.receive.median_rel_error:.3%})",
            f"ask (you buy at)  = '{self.ask_field}'",
            f"bid (you sell at) = '{self.bid_field}'",
            f"role evidence: {self.role_agreement:.1%} of {self.role_sample} two-sided lines "
            f"put '{self.ask_field}' above '{self.bid_field}'",
        ]
        lines.extend(self.notes)
        return "\n".join(lines)


@dataclass(frozen=True)
class QuoteSample:
    """One line reduced to what the resolver needs."""

    name: str
    chaos_equivalent: float
    pay_value: float | None
    receive_value: float | None


@dataclass(frozen=True)
class ScaleRelation:
    """Whether an observed series is a reference series or its reciprocal."""

    label: str
    reciprocal: bool
    usable: int
    agreement: float
    median_rel_error: float
    examples: tuple[str, ...] = ()


def infer_scale_relation(
    pairs: Iterable[tuple[str, float, float]],
    label: str,
    *,
    endpoint: str,
    min_usable: int = MIN_USABLE_LINES,
    min_agreement: float = MIN_AGREEMENT,
) -> ScaleRelation:
    """Decide whether `observed ~= reference` or `observed ~= 1 / reference`.

    Each pair is ``(name, observed, reference)``. The comparison is done in
    log space so it is scale-free, and pairs whose reference sits near 1.0
    are skipped because they cannot distinguish the two hypotheses.
    """
    direct_err: list[float] = []
    recip_err: list[float] = []
    direct_votes = 0
    usable = 0
    examples: list[str] = []

    for name, observed, reference in pairs:
        if observed is None or reference is None or observed <= 0 or reference <= 0:
            continue
        if max(reference, 1.0 / reference) < MIN_DISCRIMINATING_RATIO:
            continue

        log_o = math.log(observed)
        log_r = math.log(reference)
        d_direct = abs(log_o - log_r)
        d_recip = abs(log_o + log_r)
        direct_err.append(d_direct)
        recip_err.append(d_recip)
        usable += 1
        is_direct = d_direct <= d_recip
        direct_votes += int(is_direct)
        if len(examples) < 3:
            verdict = "direct" if is_direct else "reciprocal"
            examples.append(
                f"{name}: observed={observed:g}, reference={reference:g} -> {verdict}"
            )

    if usable < min_usable:
        raise DirectionError(
            f"cannot resolve {label}: only {usable} usable sample(s), need at least "
            f"{min_usable}. Refusing to assume a convention, because an inverted rate "
            "silently mis-scales every number downstream.",
            endpoint=endpoint,
        )

    recip_votes = usable - direct_votes
    reciprocal = recip_votes > direct_votes
    agreement = max(direct_votes, recip_votes) / usable
    if agreement < min_agreement:
        raise DirectionError(
            f"{label} is ambiguous: only {agreement:.0%} of {usable} samples agree "
            f"(need {min_agreement:.0%}). The payload shape has probably changed; "
            "re-run scripts/probe.py and update docs/SCHEMA.md.",
            endpoint=endpoint,
        )

    errs = recip_err if reciprocal else direct_err
    return ScaleRelation(
        label=label,
        reciprocal=reciprocal,
        usable=usable,
        agreement=agreement,
        # Convert the log-space residual back to a readable relative error.
        median_rel_error=math.expm1(statistics.median(errs)),
        examples=tuple(examples),
    )


def infer_denomination(
    samples: Iterable[QuoteSample],
    field_name: str,
    *,
    endpoint: str,
) -> FieldDenomination:
    """Decide whether `field_name` is quoted in chaos/unit or units/chaos.

    `chaosEquivalent` is the reference: poe.ninja documents it as the chaos
    value of one unit of the line's currency, so a DIRECT fit means the
    quote is chaos-per-unit and a reciprocal fit means units-per-chaos.
    """
    pairs = (
        (s.name, s.pay_value if field_name == "pay" else s.receive_value, s.chaos_equivalent)
        for s in samples
    )
    relation = infer_scale_relation(
        ((n, o, r) for n, o, r in pairs if o is not None),
        f"the denomination of '{field_name}'",
        endpoint=endpoint,
    )
    return FieldDenomination(
        field_name=field_name,
        denomination="units_per_chaos" if relation.reciprocal else "chaos_per_unit",
        usable_lines=relation.usable,
        agreement=relation.agreement,
        median_rel_error=relation.median_rel_error,
        examples=relation.examples,
    )


def resolve_orientation(samples: Iterable[QuoteSample], *, endpoint: str) -> Orientation:
    """Fully resolve the pay/receive convention, or raise."""
    samples = list(samples)
    pay = infer_denomination(samples, "pay", endpoint=endpoint)
    receive = infer_denomination(samples, "receive", endpoint=endpoint)

    pay_above = 0
    receive_above = 0
    for s in samples:
        if not s.pay_value or not s.receive_value:
            continue
        if s.pay_value <= 0 or s.receive_value <= 0:
            continue
        p = pay.to_chaos_per_unit(s.pay_value)
        r = receive.to_chaos_per_unit(s.receive_value)
        if p == r:
            continue
        if p > r:
            pay_above += 1
        else:
            receive_above += 1

    total = pay_above + receive_above
    if total < MIN_USABLE_LINES:
        raise DirectionError(
            f"cannot resolve which of pay/receive is the ask side: only {total} line(s) "
            f"quote both sides, need at least {MIN_USABLE_LINES}.",
            endpoint=endpoint,
        )

    role_agreement = max(pay_above, receive_above) / total
    if role_agreement < MIN_AGREEMENT:
        raise DirectionError(
            "the pay/receive roles are ambiguous: neither side is systematically the more "
            f"expensive one ({pay_above} lines favour 'pay', {receive_above} favour "
            f"'receive'; need {MIN_AGREEMENT:.0%} agreement). Spreads would be unreliable, "
            "so this run stops rather than emitting them.",
            endpoint=endpoint,
        )

    ask_field = "pay" if pay_above > receive_above else "receive"
    bid_field = "receive" if ask_field == "pay" else "pay"

    notes = (
        f"Resolved from {len(samples)} line(s) against the documented meaning of "
        "chaosEquivalent; no convention was assumed.",
    )
    return Orientation(
        pay=pay,
        receive=receive,
        ask_field=ask_field,
        bid_field=bid_field,
        role_sample=total,
        role_agreement=role_agreement,
        notes=notes,
    )
