from __future__ import annotations

from collections import deque
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import requests

from core.client import MoetClient, SourceDataError
from core.config import Settings


class FakeResponse:
    def __init__(
        self,
        *,
        text: str = "",
        payload: object | None = None,
        status_code: int = 200,
    ) -> None:
        self.text = text
        self._payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self._payload


class FakeSession:
    def __init__(self, outcomes: list[FakeResponse | Exception]) -> None:
        self.outcomes = deque(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _settings(tmp_path: Path, *, page_size: int = 2) -> Settings:
    return Settings(
        source_url="https://www.moet.gov.ae/en/essential-goods-prices-platform",
        data_dir=tmp_path,
        log_level="INFO",
        page_size=page_size,
        connect_timeout_seconds=1,
        read_timeout_seconds=2,
        max_attempts=3,
    )


def _raw_record(product_id: str, branch_id: str, price: object = "5.25") -> dict[str, object]:
    return {
        "productId": product_id,
        "branchId": branch_id,
        "subCategoryId": "24",
        "categoryName": "Bread",
        "productName": "SLICE BREAD LARGE WHITE600GM",
        "brand": "ROYAL BAKERS",
        "quantity": 600,
        "actualUnit": "Gram",
        "unit": "600.0  Gram",
        "standardizedPrice": 8.75,
        "standardizedUnit": "KG",
        "price": price,
        "storeName": "Ajman Coop",
        "branchName": "Rumailah Branch",
        "emirate": "Ajman",
    }


def test_parses_source_updated_at(tmp_path: Path) -> None:
    html = "<div>Last Updated: <span>Aug 10, 2026 | 12:00 AM</span></div>"
    session = FakeSession([FakeResponse(text=html)])
    client = MoetClient(_settings(tmp_path), session=session, sleep=lambda _: None)

    result = client.fetch_source_updated_at()

    assert result.isoformat() == "2026-08-10T00:00:00+04:00"
    assert session.calls[0]["timeout"] == (1.0, 2.0)
    assert "MOET-Price-Monitor" in session.headers["User-Agent"]


def test_fetches_all_pages_and_matches_total_count(tmp_path: Path) -> None:
    page_1 = {"totalCount": 3, "data": [_raw_record("1", "10"), _raw_record("2", "10")]}
    page_2 = {
        "totalCount": 3,
        "data": [
            _raw_record("1", "10"),
            _raw_record("2", "10"),
            _raw_record("3", "11"),
        ],
    }
    session = FakeSession([FakeResponse(payload=page_1), FakeResponse(payload=page_2)])
    client = MoetClient(_settings(tmp_path), session=session, sleep=lambda _: None)

    records = client.fetch_snapshot()

    assert [record.key for record in records] == [("1", "10"), ("2", "10"), ("3", "11")]
    namespace = "_com_moet_market_analysis_dashboard_web_MarketAnalysisDashboardWebPortlet_INSTANCE_wpmd_"
    assert [call["params"][namespace + "page"] for call in session.calls] == [1, 1]
    assert [call["params"][namespace + "pageSize"] for call in session.calls] == [2, 3]


def test_rejects_total_count_mismatch(tmp_path: Path) -> None:
    session = FakeSession(
        [
            FakeResponse(payload={"totalCount": 3, "data": [_raw_record("1", "1"), _raw_record("2", "1")]}),
            FakeResponse(
                payload={
                    "totalCount": 4,
                    "data": [
                        _raw_record("1", "1"),
                        _raw_record("2", "1"),
                        _raw_record("3", "1"),
                    ],
                }
            ),
        ]
    )
    client = MoetClient(_settings(tmp_path), session=session, sleep=lambda _: None)

    with pytest.raises(SourceDataError, match="totalCount"):
        client.fetch_snapshot()


def test_rejects_incomplete_cumulative_snapshot(tmp_path: Path) -> None:
    session = FakeSession(
        [
            FakeResponse(payload={"totalCount": 3, "data": [_raw_record("1", "1")]}),
            FakeResponse(
                payload={
                    "totalCount": 3,
                    "data": [_raw_record("1", "1"), _raw_record("2", "1")],
                }
            ),
        ]
    )
    client = MoetClient(_settings(tmp_path, page_size=1), session=session, sleep=lambda _: None)

    with pytest.raises(SourceDataError, match="complete snapshot"):
        client.fetch_snapshot()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda row: row.pop("productId"),
        lambda row: row.__setitem__("price", "not-a-price"),
    ],
)
def test_rejects_invalid_source_record(tmp_path: Path, mutator: object) -> None:
    row = _raw_record("1", "1")
    mutator(row)  # type: ignore[operator]
    session = FakeSession([FakeResponse(payload={"totalCount": 1, "data": [row]})])
    client = MoetClient(_settings(tmp_path), session=session, sleep=lambda _: None)

    with pytest.raises(SourceDataError):
        client.fetch_snapshot()


def test_deduplicates_exact_product_branch_rows(tmp_path: Path) -> None:
    row = _raw_record("1", "1")
    session = FakeSession(
        [FakeResponse(payload={"totalCount": 2, "data": [row, deepcopy(row)]})]
    )
    client = MoetClient(_settings(tmp_path), session=session, sleep=lambda _: None)

    assert len(client.fetch_snapshot()) == 1


def test_rejects_conflicting_product_branch_rows(tmp_path: Path) -> None:
    first = _raw_record("1", "1", price="5.25")
    second = _raw_record("1", "1", price="6.25")
    session = FakeSession(
        [FakeResponse(payload={"totalCount": 2, "data": [first, second]})]
    )
    client = MoetClient(_settings(tmp_path), session=session, sleep=lambda _: None)

    with pytest.raises(SourceDataError, match="conflicting"):
        client.fetch_snapshot()


def test_retries_transient_failures_and_reports_exhaustion(tmp_path: Path) -> None:
    html = "Last Updated: <span>Aug 10, 2026 | 12:00 AM</span>"
    sleeps: list[float] = []
    session = FakeSession(
        [requests.Timeout("one"), requests.ConnectionError("two"), FakeResponse(text=html)]
    )
    client = MoetClient(_settings(tmp_path), session=session, sleep=sleeps.append)

    assert client.fetch_source_updated_at().day == 10
    assert sleeps == [0.5, 1.0]

    failed = FakeSession([requests.Timeout("x"), requests.Timeout("y"), requests.Timeout("z")])
    client = MoetClient(_settings(tmp_path), session=failed, sleep=lambda _: None)
    with pytest.raises(SourceDataError, match="3 attempts"):
        client.fetch_source_updated_at()
