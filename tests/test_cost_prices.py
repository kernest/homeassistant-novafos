from custom_components.novafos.coordinator import NovafosUpdateCoordinator


def test_price_for_year_uses_own_year_then_latest_earlier_then_earliest():
    prices = {2026: 50.0, 2028: 70.0}
    price_for = NovafosUpdateCoordinator._price_for_year

    assert price_for(prices, 2026) == 50.0
    # A year without its own price keeps the latest earlier price.
    assert price_for(prices, 2027) == 50.0
    assert price_for(prices, 2028) == 70.0
    # History before the first priced year uses the earliest price.
    assert price_for(prices, 2025) == 50.0
