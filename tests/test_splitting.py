from decimal import Decimal

import pytest

from splitting import (
    Transfer, balances, format_cents, is_settled, split_by_weight, suggest_transfers, to_cents,
)


@pytest.mark.parametrize("raw, cents", [("1500", 150000), ("249.50", 24950), ("0.105", 11), (Decimal("3.3"), 330), ("1,200", 120000)])
def test_to_cents(raw, cents):
    assert to_cents(raw) == cents


@pytest.mark.parametrize("raw", ["", "abc", "NaN", "Infinity"])
def test_to_cents_rejects_non_numbers(raw):
    with pytest.raises(ValueError):
        to_cents(raw)


def test_format_cents():
    assert format_cents(123456) == "1,234.56"
    assert format_cents(-500) == "-5.00"


def test_equal_split_never_loses_a_cent():
    shares = split_by_weight(10000, {1: Decimal(1), 2: Decimal(1), 3: Decimal(1)})
    assert sum(shares.values()) == 10000
    assert sorted(shares.values()) == [3333, 3333, 3334]


def test_weighted_split():
    # Half a share for someone who skipped drinks, a double share for a couple.
    shares = split_by_weight(7000, {1: Decimal(1), 2: Decimal("0.5"), 3: Decimal(2)})
    assert shares == {1: 2000, 2: 1000, 3: 4000}


def test_zero_weight_pays_nothing_and_all_zero_is_an_error():
    assert split_by_weight(900, {1: Decimal(1), 2: Decimal(0)}) == {1: 900, 2: 0}
    with pytest.raises(ValueError):
        split_by_weight(900, {1: Decimal(0), 2: Decimal(0)})


def test_people_who_paid_nothing_still_owe_their_share():
    shares, bal = balances({1: 9000, 2: 0, 3: 0}, {1: Decimal(1), 2: Decimal(1), 3: Decimal(1)})
    assert shares == {1: 3000, 2: 3000, 3: 3000}
    assert bal == {1: 6000, 2: -3000, 3: -3000}
    assert set(suggest_transfers(bal)) == {Transfer(2, 1, 3000), Transfer(3, 1, 3000)}


def test_paid_transfers_reduce_what_is_left():
    weights = {1: Decimal(1), 2: Decimal(1)}
    _, bal = balances({1: 1000, 2: 0}, weights, [Transfer(2, 1, 300)])
    assert bal == {1: 200, 2: -200}
    _, bal = balances({1: 1000, 2: 0}, weights, [Transfer(2, 1, 500)])
    assert is_settled(1000, bal) and suggest_transfers(bal) == []


def test_transfers_always_balance_out():
    paid = {1: 12345, 2: 678, 3: 0, 4: 4321, 5: 999}
    weights = {1: Decimal(1), 2: Decimal("1.5"), 3: Decimal("0.25"), 4: Decimal(1), 5: Decimal(2)}
    _, bal = balances(paid, weights)
    assert sum(bal.values()) == 0
    for t in suggest_transfers(bal):
        bal[t.from_id] += t.amount
        bal[t.to_id] -= t.amount
    assert all(v == 0 for v in bal.values())


def test_nothing_spent_is_not_settled():
    assert not is_settled(0, {1: 0})
