"""Money logic for FairShare. Pure functions, no Flask or database, so it's easy to test.

All amounts are whole cents (int). Floats are never used for money: 0.1 + 0.2 != 0.3.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


def to_cents(amount) -> int:
    """Decimal/str/int amount in currency units -> whole cents. Raises ValueError if invalid."""
    try:
        value = Decimal(str(amount).strip().replace(",", ""))
    except Exception:
        raise ValueError("not a number")
    if not value.is_finite():
        raise ValueError("not a number")
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_cents(cents: int) -> str:
    """12345 -> '123.45', -500 -> '-5.00', with thousands separators."""
    return f"{Decimal(cents) / 100:,.2f}"


def split_by_weight(total: int, weights: dict[int, Decimal]) -> dict[int, int]:
    """Split `total` cents between people in proportion to their weights.

    Uses the largest-remainder method so the shares always add up to exactly
    `total` (no cent lost or invented to rounding). Ties go to the person
    listed first, so the result is deterministic.
    """
    weight_sum = sum(weights.values())
    if total == 0 or not weights:
        return {person: 0 for person in weights}
    if weight_sum <= 0:
        raise ValueError("At least one person needs a share above 0.")

    exact = {p: Decimal(total) * w / weight_sum for p, w in weights.items()}
    shares = {p: int(v) for p, v in exact.items()}  # round toward zero first
    leftover = total - sum(shares.values())
    by_remainder = sorted(weights, key=lambda p: exact[p] - shares[p], reverse=True)
    for p in by_remainder[:leftover]:
        shares[p] += 1
    return shares


@dataclass(frozen=True)
class Transfer:
    from_id: int
    to_id: int
    amount: int  # cents


def balances(
    paid: dict[int, int],
    weights: dict[int, Decimal],
    settled: list[Transfer] = (),
) -> tuple[dict[int, int], dict[int, int]]:
    """Return (shares, balances) per person.

    balance > 0: the group owes this person. balance < 0: they owe the group.
    A transfer already marked as paid moves money from the payer's debt to the
    receiver's credit, so it counts towards settling up.
    """
    people = set(paid) | set(weights)
    total = sum(paid.values())
    shares = split_by_weight(total, {p: weights.get(p, Decimal(1)) for p in people})
    bal = {p: paid.get(p, 0) - shares[p] for p in people}
    for t in settled:
        bal[t.from_id] = bal.get(t.from_id, 0) + t.amount
        bal[t.to_id] = bal.get(t.to_id, 0) - t.amount
    return shares, bal


def suggest_transfers(bal: dict[int, int]) -> list[Transfer]:
    """Fewest-ish transfers that bring every balance to zero.

    Greedy: the person who owes most pays the person owed most, repeatedly.
    Not always the theoretical minimum, but at most (people - 1) transfers.
    """
    creditors = {p: v for p, v in bal.items() if v > 0}
    debtors = {p: -v for p, v in bal.items() if v < 0}
    transfers = []
    while creditors and debtors:
        c = max(creditors, key=lambda p: (creditors[p], -p))
        d = max(debtors, key=lambda p: (debtors[p], -p))
        amount = min(creditors[c], debtors[d])
        transfers.append(Transfer(d, c, amount))
        creditors[c] -= amount
        debtors[d] -= amount
        if creditors[c] == 0:
            del creditors[c]
        if debtors[d] == 0:
            del debtors[d]
    return transfers


def is_settled(total_paid: int, bal: dict[int, int]) -> bool:
    """A bill is settled once something was spent and nobody owes anything."""
    return total_paid > 0 and all(v == 0 for v in bal.values())
