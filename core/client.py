"""Client for the public MOET price endpoints."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import html as html_module
import logging
import re
import time
from typing import Any

import requests

from .config import Settings


LOGGER = logging.getLogger(__name__)
UAE_TIMEZONE = timezone(timedelta(hours=4), name="Asia/Dubai")
PORTLET_ID = "com_moet_market_analysis_dashboard_web_MarketAnalysisDashboardWebPortlet_INSTANCE_wpmd"
NAMESPACE = f"_{PORTLET_ID}_"
SOURCE_FIELDS = {
    "productId",
    "branchId",
    "subCategoryId",
    "categoryName",
    "productName",
    "brand",
    "quantity",
    "actualUnit",
    "unit",
    "standardizedPrice",
    "standardizedUnit",
    "price",
    "storeName",
    "branchName",
    "emirate",
}


class SourceDataError(RuntimeError):
    """Raised when the source is incomplete or violates its expected schema."""


@dataclass(frozen=True)
class PriceRecord:
    """One normalized product-at-branch price record."""

    product_id: str
    branch_id: str
    subcategory_id: str
    category_name: str
    product_name: str
    brand: str
    quantity: str
    actual_unit: str
    unit: str
    standardized_price: str
    standardized_unit: str
    price_aed: str
    store_name: str
    branch_name: str
    emirate: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.product_id, self.branch_id)

    def as_dict(self) -> dict[str, str]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @classmethod
    def from_dict(cls, values: Mapping[str, object]) -> PriceRecord:
        return cls(**{field.name: str(values.get(field.name, "")) for field in fields(cls)})


class MoetClient:
    """Read and validate the source version and complete price snapshot."""

    def __init__(
        self,
        settings: Settings,
        *,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.sleep = sleep
        self.session.headers.update(
            {
                "User-Agent": "MOET-Price-Monitor/1.0 (+local six-hour public-data monitor)",
                "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            }
        )

    def fetch_source_updated_at(self) -> datetime:
        response = self._get(self.settings.source_url)
        decoded = html_module.unescape(response.text)
        match = re.search(
            r"Last\s+Updated:\s*(?:<[^>]+>\s*)*"
            r"([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}\s*\|\s*\d{1,2}:\d{2}\s+[AP]M)",
            decoded,
            flags=re.IGNORECASE,
        )
        if match is None:
            raise SourceDataError("Could not find the public 'Last Updated' value")
        try:
            parsed = datetime.strptime(match.group(1), "%b %d, %Y | %I:%M %p")
        except ValueError as exc:
            raise SourceDataError(f"Invalid source update timestamp: {match.group(1)!r}") from exc
        return parsed.replace(tzinfo=UAE_TIMEZONE)

    def fetch_snapshot(self) -> list[PriceRecord]:
        probe = self._fetch_page(1)
        expected_total = _parse_total_count(probe.get("totalCount"))
        probe_rows = _data_rows(probe, page=1)

        if len(probe_rows) == expected_total:
            final_rows = probe_rows
        else:
            final_payload = self._fetch_page(1, page_size=expected_total)
            final_total = _parse_total_count(final_payload.get("totalCount"))
            if final_total != expected_total:
                raise SourceDataError(
                    f"totalCount changed during collection: {expected_total} -> {final_total}"
                )
            final_rows = _data_rows(final_payload, page=1)

        if len(final_rows) != expected_total:
            raise SourceDataError(
                "MOET cumulative page did not contain the complete snapshot: "
                f"received {len(final_rows)}, declared {expected_total}"
            )

        records: list[PriceRecord] = []
        seen: dict[tuple[str, str], PriceRecord] = {}
        exact_duplicates = 0
        for index, raw_row in enumerate(final_rows, start=1):
            if not isinstance(raw_row, Mapping):
                raise SourceDataError(f"Final snapshot row {index} is not an object")
            record = _normalize_record(raw_row)
            previous = seen.get(record.key)
            if previous is not None:
                if previous != record:
                    raise SourceDataError(
                        "Source contains conflicting rows for productId + branchId: "
                        f"{record.product_id!r} + {record.branch_id!r}"
                    )
                exact_duplicates += 1
                continue
            seen[record.key] = record
            records.append(record)
        if exact_duplicates:
            LOGGER.warning(
                "Removed %s exact duplicate source rows from %s raw records",
                exact_duplicates,
                expected_total,
            )
        return records

    def _fetch_page(
        self, page: int, *, page_size: int | None = None
    ) -> Mapping[str, object]:
        params: dict[str, object] = {
            "p_p_id": PORTLET_ID,
            "p_p_lifecycle": 2,
            "p_p_state": "normal",
            "p_p_mode": "view",
            "p_p_resource_id": "/product/filter",
            "p_p_cacheability": "cacheLevelPage",
            NAMESPACE + "unit": "",
            NAMESPACE + "brand": "",
            NAMESPACE + "emirateId": "",
            NAMESPACE + "entityId": "",
            NAMESPACE + "search": "",
            NAMESPACE + "categoryId": "",
            NAMESPACE + "subCategoryId": "",
            NAMESPACE + "branchId": "",
            NAMESPACE + "quantity": "",
            NAMESPACE + "page": page,
            NAMESPACE + "pageSize": page_size or self.settings.page_size,
        }
        response = self._get(self.settings.source_url, params=params)
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise SourceDataError(f"Page {page} did not return valid JSON") from exc
        if not isinstance(payload, Mapping):
            raise SourceDataError(f"Page {page} JSON root is not an object")
        return payload

    def _get(self, url: str, **kwargs: object) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.settings.max_attempts + 1):
            try:
                response = self.session.get(url, timeout=self.settings.timeout, **kwargs)
                status_code = int(response.status_code)
                if status_code == 429 or status_code >= 500:
                    raise requests.HTTPError(f"retryable HTTP {status_code}")
                if status_code >= 400:
                    raise SourceDataError(f"HTTP {status_code} from {url}")
                return response
            except SourceDataError:
                raise
            except requests.RequestException as exc:
                last_error = exc
                LOGGER.warning(
                    "HTTP attempt %s/%s failed for %s: %s",
                    attempt,
                    self.settings.max_attempts,
                    url,
                    exc,
                )
                if attempt < self.settings.max_attempts:
                    self.sleep(0.5 * (2 ** (attempt - 1)))

        raise SourceDataError(
            f"HTTP request failed after {self.settings.max_attempts} attempts: {url}"
        ) from last_error


def _parse_total_count(value: object) -> int:
    try:
        total = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise SourceDataError(f"Invalid totalCount: {value!r}") from exc
    if total < 0:
        raise SourceDataError(f"Invalid negative totalCount: {total}")
    return total


def _data_rows(payload: Mapping[str, object], *, page: int) -> list[object]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise SourceDataError(f"Page {page} has no data list")
    return rows


def _normalize_record(raw: Mapping[str, object]) -> PriceRecord:
    missing = sorted(SOURCE_FIELDS - set(raw))
    if missing:
        raise SourceDataError(f"Source record is missing fields: {', '.join(missing)}")

    product_id = _text(raw["productId"])
    branch_id = _text(raw["branchId"])
    if not product_id or not branch_id:
        raise SourceDataError("productId and branchId must be non-empty")

    return PriceRecord(
        product_id=product_id,
        branch_id=branch_id,
        subcategory_id=_text(raw["subCategoryId"]),
        category_name=_text(raw["categoryName"]),
        product_name=_text(raw["productName"]),
        brand=_text(raw["brand"]),
        quantity=_decimal_text(raw["quantity"], field_name="quantity"),
        actual_unit=_text(raw["actualUnit"]),
        unit=_text(raw["unit"]),
        standardized_price=_decimal_text(
            raw["standardizedPrice"], field_name="standardizedPrice"
        ),
        standardized_unit=_text(raw["standardizedUnit"]),
        price_aed=_decimal_text(raw["price"], field_name="price"),
        store_name=_text(raw["storeName"]),
        branch_name=_text(raw["branchName"]),
        emirate=_text(raw["emirate"]),
    )


def _text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _decimal_text(value: object, *, field_name: str) -> str:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise SourceDataError(f"Invalid {field_name}: {value!r}") from exc
    if not number.is_finite() or number < 0:
        raise SourceDataError(f"Invalid {field_name}: {value!r}")
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"
