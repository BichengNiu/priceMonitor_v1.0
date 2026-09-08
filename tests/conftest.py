from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.client import PriceRecord


@pytest.fixture
def source_record() -> PriceRecord:
    return PriceRecord(
        product_id="13074",
        branch_id="373",
        subcategory_id="24",
        category_name="Bread",
        product_name="SLICE BREAD LARGE WHITE600GM",
        brand="ROYAL BAKERS",
        quantity="600",
        actual_unit="Gram",
        unit="600 Gram",
        standardized_price="8.75",
        standardized_unit="KG",
        price_aed="5.25",
        store_name="Ajman Coop",
        branch_name="Rumailah Branch",
        emirate="Ajman",
    )


@pytest.fixture
def fixed_now() -> datetime:
    uae_timezone = timezone(timedelta(hours=4), name="Asia/Dubai")
    return datetime(2026, 8, 11, 10, 30, tzinfo=uae_timezone)
