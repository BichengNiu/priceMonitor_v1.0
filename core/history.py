"""Append-only DuckDB panel storage and synchronized local Stata export."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from .client import PriceRecord


PANEL_FIELDS = ["publication_date", *[field.name for field in fields(PriceRecord)]]
PANEL_DECIMAL_TYPES = {
    "quantity": (7, 3),
    "standardized_price": (8, 5),
    "price_aed": (6, 3),
}
PANEL_CATEGORICAL_FIELDS = [
    field for field in PANEL_FIELDS
    if field != "publication_date" and field not in PANEL_DECIMAL_TYPES
]
PANEL_DUCKDB_TYPES = {
    "publication_date": "DATE",
    **{
        field: f"DECIMAL({precision},{scale})"
        for field, (precision, scale) in PANEL_DECIMAL_TYPES.items()
    },
    **{field: "VARCHAR" for field in PANEL_CATEGORICAL_FIELDS},
}


class HistoryError(RuntimeError):
    """Raised when durable panel data cannot be validated or recovered."""


@dataclass(frozen=True)
class PanelObservation:
    """One product-at-branch observation for one publication date."""

    publication_date: date
    record: PriceRecord


@dataclass(frozen=True)
class CollectionProvenance:
    """Source and collection timestamps for one published snapshot."""

    publication_date: date
    source_updated_at: str
    collected_at: str
    source_url: str


class HistoryStore:
    """Append snapshots to DuckDB and keep a local Stata export in sync."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.database_path = self.data_dir / "prices_panel.duckdb"
        self.panel_path = self.data_dir / "prices_panel.dta"
        self.transaction_path = self.data_dir / ".commit-transaction.json"
        self._database_pending_path = self.data_dir / ".prices_panel.pending.duckdb"
        self._panel_pending_path = self.data_dir / ".prices_panel.pending.dta"

    def recover_if_needed(self) -> None:
        """Finish a fully staged commit left by an interrupted process."""

        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.transaction_path.exists():
            self._discard_orphaned_pending_files()
            return

        manifest = _read_json(self.transaction_path)
        if set(manifest) != {
            "schema_version",
            "source_updated_at",
            "database_pending",
            "panel_pending",
        } or manifest.get("schema_version") != 2:
            raise HistoryError("Unsupported commit transaction schema")
        if (
            manifest.get("database_pending") != self._database_pending_path.name
            or manifest.get("panel_pending") != self._panel_pending_path.name
        ):
            raise HistoryError("Transaction manifest paths do not match the current schema")
        expected_version = manifest.get("source_updated_at")
        if not isinstance(expected_version, str) or not expected_version:
            raise HistoryError("Transaction manifest has no source_updated_at")

        if self._database_pending_path.exists():
            os.replace(self._database_pending_path, self.database_path)
        elif not self.database_path.exists():
            raise HistoryError(
                "Cannot recover transaction: database and staged database are missing"
            )

        if self._panel_pending_path.exists():
            os.replace(self._panel_pending_path, self.panel_path)
        elif not self.panel_path.exists():
            raise HistoryError("Cannot recover transaction: panel and staged panel are missing")

        self._validate_database()
        self._validate_stata_export()
        if self._database_source_version() != expected_version:
            raise HistoryError("Recovered database version does not match transaction manifest")
        self.transaction_path.unlink()

    def last_source_updated_at(self) -> datetime | None:
        self.recover_if_needed()
        if not self.database_path.exists():
            return None
        return _parse_aware_timestamp(
            self._database_source_version(), "source_updated_at"
        )

    def validate_current_store(self) -> None:
        """Require the current DuckDB/DTA pair; never import or repair old formats."""

        self.recover_if_needed()
        database_exists = self.database_path.exists()
        panel_exists = self.panel_path.exists()
        if not database_exists and not panel_exists:
            return
        if database_exists != panel_exists:
            raise HistoryError(
                "The current DuckDB and DTA files must both be present; "
                "automatic cross-format migration is disabled"
            )
        self._validate_database()
        self._validate_stata_export()

    def plan_observations(
        self, snapshot: list[PriceRecord], source_updated_at: datetime
    ) -> list[PanelObservation]:
        _require_aware(source_updated_at, "source_updated_at")
        current = _records_by_key(snapshot)
        return [
            PanelObservation(source_updated_at.date(), current[key])
            for key in sorted(current)
        ]

    def commit(
        self,
        observations: list[PanelObservation],
        snapshot: list[PriceRecord],
        source_updated_at: datetime,
        *,
        collected_at: datetime,
        source_url: str,
    ) -> None:
        """Stage complete outputs, then publish or recover them deterministically."""

        _require_aware(source_updated_at, "source_updated_at")
        _require_aware(collected_at, "collected_at")
        source_url = source_url.strip()
        if not source_url:
            raise ValueError("source_url must not be empty")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.recover_if_needed()
        _records_by_key(snapshot)

        try:
            if self.database_path.exists():
                self._validate_database()
                self._validate_stata_export()
                existing = self._read_database()
                provenance = self._read_collection_provenance()
            elif self.panel_path.exists():
                raise HistoryError(
                    "DuckDB is missing; automatic DTA-to-DuckDB migration is disabled"
                )
            else:
                existing = _empty_panel()
                provenance = []
            addition = _observations_frame(observations)
            combined = pd.concat([existing, addition], ignore_index=True)
            if combined.duplicated(
                subset=["publication_date", "product_id", "branch_id"]
            ).any():
                raise HistoryError(
                    "Duplicate panel identity: publication_date + product_id + branch_id"
                )
            provenance.append(
                CollectionProvenance(
                    publication_date=source_updated_at.date(),
                    source_updated_at=source_updated_at.isoformat(timespec="seconds"),
                    collected_at=collected_at.isoformat(timespec="seconds"),
                    source_url=source_url,
                )
            )
            _validate_collection_provenance(provenance)
            _write_duckdb(
                self._database_pending_path,
                combined,
                source_updated_at.isoformat(timespec="seconds"),
                provenance,
            )
            _write_stata(self._panel_pending_path, combined)
            self._write_transaction_manifest(source_updated_at)
        except Exception:
            if not self.transaction_path.exists():
                self._discard_orphaned_pending_files()
            raise
        self.recover_if_needed()

    def _validate_database(self) -> None:
        try:
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                description = connection.execute("DESCRIBE prices_panel").fetchall()
                columns = [row[0] for row in description]
                actual_types = {row[0]: row[1] for row in description}
                tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
                if "monitor_metadata" not in tables:
                    raise HistoryError("DuckDB monitor_metadata table is required")
                if "collection_metadata" not in tables:
                    raise HistoryError("DuckDB collection_metadata table is required")
                metadata_columns = [
                    (row[0], row[1])
                    for row in connection.execute(
                        "DESCRIBE monitor_metadata"
                    ).fetchall()
                ]
                versions = connection.execute(
                    "SELECT source_updated_at FROM monitor_metadata"
                ).fetchall()
                provenance_columns = [
                    (row[0], row[1])
                    for row in connection.execute(
                        "DESCRIBE collection_metadata"
                    ).fetchall()
                ]
                provenance_rows = connection.execute(
                    "SELECT publication_date, source_updated_at, collected_at, source_url "
                    "FROM collection_metadata"
                ).fetchall()
                latest_date = connection.execute(
                    "SELECT MAX(publication_date) FROM prices_panel"
                ).fetchone()[0]
        except (OSError, duckdb.Error) as exc:
            raise HistoryError(f"Cannot read valid DuckDB data from {self.database_path}") from exc
        if columns != PANEL_FIELDS:
            raise HistoryError("DuckDB panel columns do not match the approved schema")
        if actual_types != PANEL_DUCKDB_TYPES:
            raise HistoryError(
                "DuckDB panel types do not match the current optimized schema; "
                "automatic schema migration is disabled"
            )
        if metadata_columns != [("source_updated_at", "VARCHAR")]:
            raise HistoryError("DuckDB monitor metadata schema is not current")
        if provenance_columns != [
            ("publication_date", "DATE"),
            ("source_updated_at", "VARCHAR"),
            ("collected_at", "VARCHAR"),
            ("source_url", "VARCHAR"),
        ]:
            raise HistoryError("DuckDB collection metadata schema is not current")
        _validate_collection_provenance(
            [CollectionProvenance(*row) for row in provenance_rows]
        )
        if len(versions) != 1 or not isinstance(versions[0][0], str):
            raise HistoryError("DuckDB monitor metadata must contain one source version")
        source_version = versions[0][0]
        _parse_aware_timestamp(source_version, "source_updated_at")
        if latest_date is not None and _parse_aware_timestamp(
            source_version, "source_updated_at"
        ).date().isoformat() != str(latest_date):
            raise HistoryError("DuckDB source metadata does not match its latest panel date")

    def _database_source_version(self) -> str:
        try:
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                values = connection.execute(
                    "SELECT source_updated_at FROM monitor_metadata"
                ).fetchall()
        except (OSError, duckdb.Error) as exc:
            raise HistoryError(f"Cannot read DuckDB source version in {self.database_path}") from exc
        if len(values) != 1:
            raise HistoryError("DuckDB monitor metadata must contain one source version")
        value = values[0][0]
        if not isinstance(value, str):
            raise HistoryError("DuckDB source_updated_at must be a string")
        _parse_aware_timestamp(value, "source_updated_at")
        return value

    def _validate_stata_export(self) -> None:
        try:
            compact = _stata_has_compact_schema(self.panel_path)
        except (OSError, ValueError, KeyError) as exc:
            raise HistoryError(f"Cannot read valid Stata data from {self.panel_path}") from exc
        if not compact:
            raise HistoryError(
                "Stata export does not match the current optimized schema; "
                "automatic conversion or repair is disabled"
            )
        try:
            panel = _normalize_panel_values(
                pd.read_stata(self.panel_path, convert_categoricals=True)
            )
            _validate_panel_frame(panel)
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                database_count, database_latest = connection.execute(
                    "SELECT COUNT(*), MAX(publication_date) FROM prices_panel"
                ).fetchone()
        except (OSError, ValueError, duckdb.Error) as exc:
            raise HistoryError(f"Cannot validate synchronized Stata export {self.panel_path}") from exc
        panel_latest = (
            str(panel["publication_date"].max()) if not panel.empty else None
        )
        database_latest_text = (
            str(database_latest) if database_latest is not None else None
        )
        if len(panel) != int(database_count) or panel_latest != database_latest_text:
            raise HistoryError(
                "Stata export is out of sync with DuckDB; automatic repair is disabled"
            )

    def _read_database(self) -> pd.DataFrame:
        try:
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                frame = connection.execute("SELECT * FROM prices_panel").fetchdf()
        except (OSError, duckdb.Error, ValueError) as exc:
            raise HistoryError(f"Cannot read valid DuckDB data from {self.database_path}") from exc
        frame = _normalize_panel_values(frame)
        _validate_panel_frame(frame)
        return frame[PANEL_FIELDS]

    def _read_collection_provenance(self) -> list[CollectionProvenance]:
        try:
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                rows = connection.execute(
                    "SELECT publication_date, source_updated_at, collected_at, source_url "
                    "FROM collection_metadata ORDER BY publication_date"
                ).fetchall()
        except (OSError, duckdb.Error) as exc:
            raise HistoryError(
                f"Cannot read DuckDB collection metadata in {self.database_path}"
            ) from exc
        return [CollectionProvenance(*row) for row in rows]

    def _write_transaction_manifest(self, source_updated_at: datetime) -> None:
        temporary_manifest = self.transaction_path.with_suffix(".json.tmp")
        _write_json(
            temporary_manifest,
            {
                "schema_version": 2,
                "source_updated_at": source_updated_at.isoformat(timespec="seconds"),
                "database_pending": self._database_pending_path.name,
                "panel_pending": self._panel_pending_path.name,
            },
        )
        os.replace(temporary_manifest, self.transaction_path)

    def _discard_orphaned_pending_files(self) -> None:
        for path in (
            self._database_pending_path,
            self._database_pending_path.with_name(self._database_pending_path.name + ".wal"),
            self._panel_pending_path,
        ):
            path.unlink(missing_ok=True)


def _empty_panel() -> pd.DataFrame:
    return pd.DataFrame(columns=PANEL_FIELDS, dtype=str)


def _observations_frame(observations: list[PanelObservation]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "publication_date": observation.publication_date.isoformat(),
                **observation.record.as_dict(),
            }
            for observation in observations
        ],
        columns=PANEL_FIELDS,
        dtype=str,
    )


def _validate_panel_frame(frame: pd.DataFrame) -> None:
    if list(frame.columns) != PANEL_FIELDS:
        raise HistoryError("Stata panel variables do not match the approved schema")
    if not frame.empty and not frame["publication_date"].astype(str).str.fullmatch(
        r"\d{4}-\d{2}-\d{2}"
    ).all():
        raise HistoryError("Panel publication_date must use YYYY-MM-DD")
    if frame.duplicated(subset=["publication_date", "product_id", "branch_id"]).any():
        raise HistoryError(
            "Duplicate panel identity: publication_date + product_id + branch_id"
        )
    for column, (precision, scale) in PANEL_DECIMAL_TYPES.items():
        for value in frame[column]:
            try:
                number = Decimal(str(value))
            except (InvalidOperation, ValueError) as exc:
                raise HistoryError(f"Panel {column} must contain decimal values") from exc
            if not number.is_finite() or number < 0:
                raise HistoryError(f"Panel {column} contains an invalid value: {value!r}")
            integer_digits = max(number.adjusted() + 1, 1) if number else 1
            actual_scale = max(-number.as_tuple().exponent, 0)
            if actual_scale > scale or integer_digits + scale > precision:
                raise HistoryError(
                    f"Panel {column} value {value!r} exceeds DECIMAL({precision},{scale})"
                )


def _validate_collection_provenance(
    provenance: list[CollectionProvenance],
) -> None:
    seen_dates: set[date] = set()
    for row in provenance:
        if (
            not isinstance(row.publication_date, date)
            or not isinstance(row.source_updated_at, str)
            or not isinstance(row.collected_at, str)
            or not isinstance(row.source_url, str)
            or not row.source_url.strip()
        ):
            raise HistoryError("DuckDB collection metadata contains invalid values")
        if row.publication_date in seen_dates:
            raise HistoryError("DuckDB collection metadata has duplicate publication dates")
        seen_dates.add(row.publication_date)
        if (
            _parse_aware_timestamp(row.source_updated_at, "source_updated_at").date()
            != row.publication_date
        ):
            raise HistoryError(
                "DuckDB collection metadata date does not match source update"
            )
        _parse_aware_timestamp(row.collected_at, "collected_at")


def _write_stata(path: Path, frame: pd.DataFrame) -> None:
    normalized = _normalize_panel_values(frame)
    _validate_panel_frame(normalized)
    stata_frame = normalized.copy()
    stata_frame["publication_date"] = pd.to_datetime(
        stata_frame["publication_date"], format="%Y-%m-%d", errors="raise"
    )
    for column in PANEL_DECIMAL_TYPES:
        stata_frame[column] = pd.to_numeric(stata_frame[column], errors="raise").astype(
            "float64"
        )
    for column in PANEL_CATEGORICAL_FIELDS:
        stata_frame[column] = stata_frame[column].astype("category")
    stata_frame.to_stata(
        path,
        write_index=False,
        version=118,
        convert_dates={"publication_date": "td"},
    )
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _write_duckdb(
    path: Path,
    frame: pd.DataFrame,
    source_updated_at: str,
    provenance: list[CollectionProvenance],
) -> None:
    """Write a compact panel using native date/decimal types and string dimensions."""

    normalized = _normalize_panel_values(frame)
    _validate_panel_frame(normalized)
    connection = duckdb.connect(str(path))
    try:
        connection.register("_panel_frame", normalized)
        select_fields = []
        for field in PANEL_FIELDS:
            quoted = f'"{field}"'
            if field == "publication_date":
                select_fields.append(f"CAST({quoted} AS DATE) AS {quoted}")
            elif field in PANEL_DECIMAL_TYPES:
                precision, scale = PANEL_DECIMAL_TYPES[field]
                select_fields.append(
                    f"CAST({quoted} AS DECIMAL({precision},{scale})) AS {quoted}"
                )
            else:
                select_fields.append(quoted)
        connection.execute(
            "CREATE TABLE prices_panel AS SELECT "
            + ", ".join(select_fields)
            + " FROM _panel_frame"
        )
        connection.execute(
            "CREATE TABLE monitor_metadata (source_updated_at VARCHAR NOT NULL)"
        )
        connection.execute("INSERT INTO monitor_metadata VALUES (?)", [source_updated_at])
        connection.execute(
            "CREATE TABLE collection_metadata ("
            "publication_date DATE NOT NULL, "
            "source_updated_at VARCHAR NOT NULL, "
            "collected_at VARCHAR NOT NULL, "
            "source_url VARCHAR NOT NULL)"
        )
        if provenance:
            connection.executemany(
                "INSERT INTO collection_metadata VALUES (?, ?, ?, ?)",
                [
                    (
                        row.publication_date,
                        row.source_updated_at,
                        row.collected_at,
                        row.source_url,
                    )
                    for row in provenance
                ],
            )
        connection.execute("CHECKPOINT")
    finally:
        connection.close()
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _normalize_panel_values(frame: pd.DataFrame) -> pd.DataFrame:
    """Decode compact Stata labels and normalize values to canonical panel text."""

    if list(frame.columns) != PANEL_FIELDS:
        raise HistoryError("Panel fields do not match the current approved schema")
    normalized = frame[PANEL_FIELDS].copy()
    for column in PANEL_FIELDS:
        if column == "publication_date":
            normalized[column] = normalized[column].map(_date_value_text)
        elif column in PANEL_DECIMAL_TYPES:
            scale = PANEL_DECIMAL_TYPES[column][1]
            normalized[column] = normalized[column].map(
                lambda value: _decimal_value_text(value, scale=scale)
            )
        else:
            normalized[column] = normalized[column].map(
                lambda value: "" if pd.isna(value) else str(value)
            )
    return normalized


def _date_value_text(value: object) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        try:
            return datetime.fromisoformat(text).date().isoformat()
        except ValueError as exc:
            raise HistoryError(f"Invalid panel publication_date: {value!r}") from exc


def _decimal_value_text(value: object, *, scale: int | None = None) -> str:
    if pd.isna(value):
        return ""
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise HistoryError(f"Invalid panel decimal value: {value!r}") from exc
    if not number.is_finite():
        raise HistoryError(f"Invalid panel decimal value: {value!r}")
    if scale is not None and isinstance(value, (float, np.floating)):
        number = number.quantize(Decimal(1).scaleb(-scale))
    rendered = format(number.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _stata_has_compact_schema(path: Path) -> bool:
    """Inspect the DTA schema from a one-row read without loading its full data."""

    reader = pd.read_stata(path, convert_categoricals=True, chunksize=1)
    try:
        sample = next(iter(reader))
    except StopIteration:
        return False
    finally:
        reader._close_file()
    if list(sample.columns) != PANEL_FIELDS:
        return False
    return (
        pd.api.types.is_datetime64_any_dtype(sample["publication_date"])
        and all(
            pd.api.types.is_float_dtype(sample[column])
            for column in PANEL_DECIMAL_TYPES
        )
        and all(
            isinstance(sample[column].dtype, pd.CategoricalDtype)
            for column in PANEL_CATEGORICAL_FIELDS
        )
    )


def _records_by_key(records: list[PriceRecord]) -> dict[str, PriceRecord]:
    indexed: dict[str, PriceRecord] = {}
    for record in records:
        key = _encode_key(record.key)
        if key in indexed:
            raise ValueError(
                "Duplicate snapshot identity: "
                f"product_id={record.product_id!r}, branch_id={record.branch_id!r}"
            )
        indexed[key] = record
    return indexed


def _encode_key(key: tuple[str, str]) -> str:
    return json.dumps(key, ensure_ascii=False, separators=(",", ":"))


def _parse_aware_timestamp(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HistoryError(f"Invalid {name}: {value!r}") from exc
    _require_aware(parsed, name)
    return parsed


def _write_json(path: Path, value: object) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HistoryError(f"Cannot read valid JSON from {path}") from exc
    if not isinstance(value, dict):
        raise HistoryError(f"JSON root must be an object: {path}")
    return value


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
