"""Append-only DuckDB panel storage, local Stata export, and recoverable state."""

from __future__ import annotations

import csv
from dataclasses import dataclass, fields
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

import duckdb
import pandas as pd

from .client import PriceRecord


PANEL_FIELDS = ["publication_date", *[field.name for field in fields(PriceRecord)]]
PANEL_CSV_V1_FIELDS = [
    "source_updated_at",
    "collected_at",
    *[field.name for field in fields(PriceRecord)],
    "source_url",
]
LEGACY_EVENT_FIELDS = [
    "change_type",
    "source_updated_at",
    "collected_at",
    "source_url",
    *[field.name for field in fields(PriceRecord)],
]


class HistoryError(RuntimeError):
    """Raised when durable panel data or state cannot be validated or recovered."""


@dataclass(frozen=True)
class PanelObservation:
    """One product-at-branch observation for one publication date."""

    publication_date: date
    record: PriceRecord


class HistoryStore:
    """Append snapshots to DuckDB and keep a local Stata export in sync."""

    def __init__(self, data_dir: Path, source_url: str) -> None:
        self.data_dir = data_dir.resolve()
        self.source_url = source_url
        self.database_path = self.data_dir / "prices_panel.duckdb"
        self.panel_path = self.data_dir / "prices_panel.dta"
        self.panel_csv_path = self.data_dir / "prices_panel.csv"
        self.legacy_event_path = self.data_dir / "prices_history.csv"
        self.state_path = self.data_dir / "state.json"
        self.transaction_path = self.data_dir / ".commit-transaction.json"
        self._database_pending_path = self.data_dir / ".prices_panel.pending.duckdb"
        self._panel_pending_path = self.data_dir / ".prices_panel.pending.dta"
        self._state_pending_path = self.data_dir / ".state.json.pending"

    def recover_if_needed(self) -> None:
        """Finish a fully staged commit left by an interrupted process."""

        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.transaction_path.exists():
            self._discard_orphaned_pending_files()
            return

        manifest = _read_json(self.transaction_path)
        expected_version = manifest.get("source_updated_at")
        if not isinstance(expected_version, str) or not expected_version:
            raise HistoryError("Transaction manifest has no source_updated_at")

        if "database_pending" in manifest:
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

        if self._state_pending_path.exists():
            os.replace(self._state_pending_path, self.state_path)
        elif not self.state_path.exists():
            raise HistoryError("Cannot recover transaction: state and staged state are missing")

        state = _read_json(self.state_path)
        if state.get("source_updated_at") != expected_version:
            raise HistoryError("Recovered state version does not match transaction manifest")
        self.transaction_path.unlink()

    def last_source_updated_at(self) -> datetime | None:
        self.recover_if_needed()
        state = self._load_state()
        if state is None and self.database_path.exists():
            self._restore_local_state()
            state = self._load_state()
        if state is None:
            return None
        value = state.get("source_updated_at")
        if not isinstance(value, str):
            raise HistoryError("State source_updated_at must be a string")
        return _parse_aware_timestamp(value, "source_updated_at")

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
    ) -> None:
        """Stage complete outputs, then publish or recover them deterministically."""

        _require_aware(source_updated_at, "source_updated_at")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.recover_if_needed()
        current = _records_by_key(snapshot)

        try:
            if self.database_path.exists():
                existing = self._read_database()
            elif self.panel_path.exists():
                existing = self._read_panel()
            else:
                existing = _empty_panel()
            addition = _observations_frame(observations)
            combined = pd.concat([existing, addition], ignore_index=True)
            if combined.duplicated(
                subset=["publication_date", "product_id", "branch_id"]
            ).any():
                raise HistoryError(
                    "Duplicate panel identity: publication_date + product_id + branch_id"
                )
            _write_duckdb(
                self._database_pending_path,
                combined,
                source_updated_at.isoformat(timespec="seconds"),
            )
            _write_stata(self._panel_pending_path, combined)
            _write_json(
                self._state_pending_path,
                {
                    "schema_version": 1,
                    "source_updated_at": source_updated_at.isoformat(timespec="seconds"),
                    "record_count": len(current),
                    "records": {key: current[key].as_dict() for key in sorted(current)},
                    "fingerprints": {
                        key: _fingerprint(current[key]) for key in sorted(current)
                    },
                },
            )
            self._write_transaction_manifest(source_updated_at)
        except Exception:
            if not self.transaction_path.exists():
                self._discard_orphaned_pending_files()
            raise
        self.recover_if_needed()

    def migrate_existing_data_if_needed(self) -> int:
        """Create a missing DuckDB database or local Stata export from existing data."""

        self.recover_if_needed()
        if self.database_path.exists():
            self._validate_database()
            database_count, database_latest_date = self._database_summary()
            if self.panel_path.exists():
                local_panel = self._read_panel()
                local_latest_date = (
                    str(local_panel["publication_date"].max())
                    if not local_panel.empty
                    else None
                )
                if (
                    len(local_panel) == database_count
                    and local_latest_date == database_latest_date
                ):
                    return 0
            frame = self._read_database()
            _write_stata(self._panel_pending_path, frame)
            os.replace(self._panel_pending_path, self.panel_path)
            return len(frame)
        if self.panel_path.exists():
            frame = self._read_panel()
            _write_duckdb(
                self._database_pending_path,
                frame,
                self._source_version_for_frame(frame),
            )
            os.replace(self._database_pending_path, self.database_path)
            return len(frame)
        if self.panel_csv_path.exists():
            frame = pd.read_csv(
                self.panel_csv_path, dtype=str, keep_default_na=False, encoding="utf-8-sig"
            )
            if list(frame.columns) == PANEL_CSV_V1_FIELDS:
                frame.insert(
                    0,
                    "publication_date",
                    frame["source_updated_at"].map(_publication_date_text),
                )
                frame = frame[PANEL_FIELDS]
            elif list(frame.columns) != PANEL_FIELDS:
                raise HistoryError("Existing panel CSV header is not a supported schema")
            _validate_panel_frame(frame)
            self._write_initial_panel(frame)
            return len(frame)
        if not self.legacy_event_path.exists():
            return 0

        state = self._load_state()
        if state is None:
            return 0
        expected_latest = _state_records(state)
        expected_version = state.get("source_updated_at")
        if not isinstance(expected_version, str):
            raise HistoryError("State source_updated_at must be a string")

        current: dict[str, PriceRecord] = {}
        active_version: str | None = None
        panel_rows: list[dict[str, str]] = []
        with self.legacy_event_path.open(
            "r", encoding="utf-8-sig", newline=""
        ) as source:
            reader = csv.DictReader(source)
            if reader.fieldnames != LEGACY_EVENT_FIELDS:
                raise HistoryError("Legacy CSV header does not match the event schema")
            for row in reader:
                version = row["source_updated_at"]
                if active_version is not None and version != active_version:
                    panel_rows.extend(_panel_period_rows(current, active_version))
                active_version = version
                record = PriceRecord.from_dict(row)
                key = _encode_key(record.key)
                change_type = row["change_type"]
                if change_type == "removed":
                    current.pop(key, None)
                elif change_type in {"initial", "new", "changed"}:
                    current[key] = record
                else:
                    raise HistoryError(f"Unknown legacy change_type: {change_type!r}")

        if active_version is None:
            raise HistoryError("Legacy CSV contains no observations")
        panel_rows.extend(_panel_period_rows(current, active_version))
        if active_version != expected_version or current != expected_latest:
            raise HistoryError("Legacy CSV does not reconstruct to the saved latest state")
        frame = pd.DataFrame(panel_rows, columns=PANEL_FIELDS)
        _validate_panel_frame(frame)
        self._write_initial_panel(frame, expected_version)
        return len(frame)

    def _write_initial_panel(
        self, frame: pd.DataFrame, source_updated_at: str | None = None
    ) -> None:
        """Write both formats when migrating data without an active transaction."""

        version = source_updated_at or self._source_version_for_frame(frame)
        _write_duckdb(self._database_pending_path, frame, version)
        _write_stata(self._panel_pending_path, frame)
        os.replace(self._database_pending_path, self.database_path)
        os.replace(self._panel_pending_path, self.panel_path)

    def _source_version_for_frame(self, frame: pd.DataFrame) -> str:
        state = self._load_state()
        if state is not None:
            value = state.get("source_updated_at")
            if isinstance(value, str):
                _parse_aware_timestamp(value, "source_updated_at")
                return value
        if frame.empty:
            raise HistoryError("Cannot determine source version for an empty panel")
        latest_date = str(frame["publication_date"].max())
        inferred = f"{latest_date}T00:00:00+04:00"
        _parse_aware_timestamp(inferred, "source_updated_at")
        return inferred

    def _validate_database(self) -> None:
        try:
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                columns = [
                    row[0]
                    for row in connection.execute("DESCRIBE prices_panel").fetchall()
                ]
                connection.execute("SELECT COUNT(*) FROM prices_panel").fetchone()
        except (OSError, duckdb.Error) as exc:
            raise HistoryError(f"Cannot read valid DuckDB data from {self.database_path}") from exc
        if columns != PANEL_FIELDS:
            raise HistoryError("DuckDB panel columns do not match the approved schema")
        self._ensure_database_metadata()
        try:
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                source_version = connection.execute(
                    "SELECT source_updated_at FROM monitor_metadata"
                ).fetchone()[0]
                latest_date = connection.execute(
                    "SELECT MAX(publication_date) FROM prices_panel"
                ).fetchone()[0]
        except (OSError, duckdb.Error) as exc:
            raise HistoryError(f"Cannot validate DuckDB metadata in {self.database_path}") from exc
        if latest_date is not None and _parse_aware_timestamp(
            source_version, "source_updated_at"
        ).date().isoformat() != latest_date:
            raise HistoryError("DuckDB source metadata does not match its latest panel date")

    def _database_summary(self) -> tuple[int, str | None]:
        try:
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                count, latest_date = connection.execute(
                    "SELECT COUNT(*), MAX(publication_date) FROM prices_panel"
                ).fetchone()
        except (OSError, duckdb.Error) as exc:
            raise HistoryError(f"Cannot summarize DuckDB panel {self.database_path}") from exc
        return int(count), str(latest_date) if latest_date is not None else None

    def _ensure_database_metadata(self) -> None:
        try:
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
                if "monitor_metadata" in tables:
                    versions = connection.execute(
                        "SELECT source_updated_at FROM monitor_metadata"
                    ).fetchall()
                    if len(versions) != 1 or not isinstance(versions[0][0], str):
                        raise HistoryError("DuckDB monitor metadata must contain one source version")
                    _parse_aware_timestamp(versions[0][0], "source_updated_at")
                    return
                latest_date = connection.execute(
                    "SELECT MAX(publication_date) FROM prices_panel"
                ).fetchone()[0]
        except (OSError, duckdb.Error) as exc:
            raise HistoryError(f"Cannot inspect DuckDB metadata in {self.database_path}") from exc

        version = self._saved_source_version()
        if version is None:
            if latest_date is None:
                raise HistoryError("DuckDB panel has no source version metadata or observations")
            version = f"{latest_date}T00:00:00+04:00"
        _parse_aware_timestamp(version, "source_updated_at")
        try:
            with duckdb.connect(str(self.database_path)) as connection:
                connection.execute(
                    "CREATE TABLE monitor_metadata (source_updated_at VARCHAR NOT NULL)"
                )
                connection.execute("INSERT INTO monitor_metadata VALUES (?)", [version])
                connection.execute("CHECKPOINT")
        except (OSError, duckdb.Error) as exc:
            raise HistoryError(f"Cannot initialize DuckDB metadata in {self.database_path}") from exc

    def _saved_source_version(self) -> str | None:
        state = self._load_state()
        if state is None:
            return None
        value = state.get("source_updated_at")
        if not isinstance(value, str):
            raise HistoryError("State source_updated_at must be a string")
        _parse_aware_timestamp(value, "source_updated_at")
        return value

    def _restore_local_state(self) -> None:
        """Rebuild ignored runtime state from the shared DuckDB snapshot."""

        self._validate_database()
        try:
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                source_updated_at = connection.execute(
                    "SELECT source_updated_at FROM monitor_metadata"
                ).fetchone()[0]
                latest_date = connection.execute(
                    "SELECT MAX(publication_date) FROM prices_panel"
                ).fetchone()[0]
                if latest_date is None:
                    raise HistoryError("Cannot restore state from an empty DuckDB panel")
                columns = ", ".join(f'"{field}"' for field in PANEL_FIELDS[1:])
                frame = connection.execute(
                    f"SELECT {columns} FROM prices_panel WHERE publication_date = ?",
                    [latest_date],
                ).fetchdf()
        except (OSError, duckdb.Error, ValueError) as exc:
            raise HistoryError(f"Cannot restore runtime state from {self.database_path}") from exc

        records = {
            _encode_key(record.key): record
            for record in (PriceRecord.from_dict(row) for row in frame.to_dict("records"))
        }
        restored = {
            "schema_version": 1,
            "source_updated_at": source_updated_at,
            "record_count": len(records),
            "records": {key: records[key].as_dict() for key in sorted(records)},
            "fingerprints": {
                key: _fingerprint(records[key]) for key in sorted(records)
            },
        }
        _write_json(self._state_pending_path, restored)
        os.replace(self._state_pending_path, self.state_path)

    def _read_panel(self) -> pd.DataFrame:
        try:
            frame = pd.read_stata(self.panel_path, convert_categoricals=False)
        except (OSError, ValueError) as exc:
            raise HistoryError(f"Cannot read valid Stata data from {self.panel_path}") from exc
        for column in frame.columns:
            frame[column] = frame[column].fillna("").astype(str)
        _validate_panel_frame(frame)
        return frame[PANEL_FIELDS]

    def _read_database(self) -> pd.DataFrame:
        try:
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                frame = connection.execute("SELECT * FROM prices_panel").fetchdf()
        except (OSError, duckdb.Error, ValueError) as exc:
            raise HistoryError(f"Cannot read valid DuckDB data from {self.database_path}") from exc
        for column in frame.columns:
            frame[column] = frame[column].fillna("").astype(str)
        _validate_panel_frame(frame)
        return frame[PANEL_FIELDS]

    def _load_state(self) -> dict[str, object] | None:
        if not self.state_path.exists():
            return None
        state = _read_json(self.state_path)
        if state.get("schema_version") != 1:
            raise HistoryError("Unsupported or missing state schema_version")
        records = state.get("records")
        fingerprints = state.get("fingerprints")
        if not isinstance(records, dict) or not isinstance(fingerprints, dict):
            raise HistoryError("State records and fingerprints must be objects")
        if state.get("record_count") != len(records) or set(records) != set(fingerprints):
            raise HistoryError("State record_count, records, and fingerprints disagree")
        for key, raw_record in records.items():
            if not isinstance(key, str) or not isinstance(raw_record, dict):
                raise HistoryError("State contains an invalid record entry")
            record = PriceRecord.from_dict(raw_record)
            if fingerprints.get(key) != _fingerprint(record):
                raise HistoryError(f"State fingerprint mismatch for {key}")
        return state

    def _write_transaction_manifest(self, source_updated_at: datetime) -> None:
        temporary_manifest = self.transaction_path.with_suffix(".json.tmp")
        _write_json(
            temporary_manifest,
            {
                "schema_version": 1,
                "source_updated_at": source_updated_at.isoformat(timespec="seconds"),
                "database_pending": self._database_pending_path.name,
                "panel_pending": self._panel_pending_path.name,
                "state_pending": self._state_pending_path.name,
            },
        )
        os.replace(temporary_manifest, self.transaction_path)

    def _discard_orphaned_pending_files(self) -> None:
        for path in (
            self._database_pending_path,
            self._database_pending_path.with_name(self._database_pending_path.name + ".wal"),
            self._panel_pending_path,
            self._state_pending_path,
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


def _panel_period_rows(
    records: Mapping[str, PriceRecord], source_updated_at: str
) -> list[dict[str, str]]:
    publication_date = _publication_date_text(source_updated_at)
    return [
        {"publication_date": publication_date, **records[key].as_dict()}
        for key in sorted(records)
    ]


def _publication_date_text(value: str) -> str:
    return _parse_aware_timestamp(value, "source_updated_at").date().isoformat()


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


def _write_stata(path: Path, frame: pd.DataFrame) -> None:
    normalized = frame[PANEL_FIELDS].fillna("").astype(str)
    normalized.to_stata(path, write_index=False, version=118)
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _write_duckdb(path: Path, frame: pd.DataFrame, source_updated_at: str) -> None:
    """Write a compact, persistent DuckDB panel with the canonical string schema."""

    normalized = frame[PANEL_FIELDS].fillna("").astype(str)
    connection = duckdb.connect(str(path))
    try:
        connection.register("_panel_frame", normalized)
        connection.execute("CREATE TABLE prices_panel AS SELECT * FROM _panel_frame")
        connection.execute(
            "CREATE TABLE monitor_metadata (source_updated_at VARCHAR NOT NULL)"
        )
        connection.execute("INSERT INTO monitor_metadata VALUES (?)", [source_updated_at])
        connection.execute("CHECKPOINT")
    finally:
        connection.close()
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


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


def _state_records(state: Mapping[str, object]) -> dict[str, PriceRecord]:
    raw_records = state.get("records")
    if not isinstance(raw_records, dict):
        raise HistoryError("State records must be an object")
    return {
        str(key): PriceRecord.from_dict(raw_record)
        for key, raw_record in raw_records.items()
        if isinstance(raw_record, Mapping)
    }


def _encode_key(key: tuple[str, str]) -> str:
    return json.dumps(key, ensure_ascii=False, separators=(",", ":"))


def _fingerprint(record: PriceRecord) -> str:
    canonical = json.dumps(
        record.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


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
