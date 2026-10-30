from src.pricing import final_price
from src.checkout import create_checkout_total


def test_regular_discount_is_applied():
    assert final_price(100, 15) == 85


def test_full_discount_can_make_total_zero():
    assert final_price(100, 100) == 0


def test_discount_must_not_make_negative_charge():
    assert final_price(10, 15) == 0


def test_checkout_uses_final_price():
    assert create_checkout_total(10, 15)["total"] == 0
