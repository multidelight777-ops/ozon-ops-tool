def calculate_approved_price(current_price: float) -> tuple[float, float]:
    """Рассчитать фиксированную скидку 3% и итоговую цену."""
    approved_discount_percent = 3.0
    approved_price = round(current_price * 0.97, 2)
    return approved_discount_percent, approved_price
