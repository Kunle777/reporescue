from .pricing import final_price


def create_checkout_total(subtotal: float, discount: float) -> dict[str, float]:
    """Small second module to demonstrate a multi-file repository layout."""
    return {"subtotal": subtotal, "total": final_price(subtotal, discount)}
