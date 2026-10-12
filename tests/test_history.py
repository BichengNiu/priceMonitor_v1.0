from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import os
from pathlib import Path

import pytest
import pandas as pd
import duckdb

from core.client import PriceRecord
from core.history import (
    PANEL_FIELDS,
    HistoryError,
    HistoryStore,
    _stata_has_compact_schema,
    _normalize_panel_values,
)


def _read_rows(path: Path) -> list[dict[str, str]]:
    frame = _normalize_panel_values(
        pd.read_stata(path, convert_categoricals=True)
    )
    return frame.to_dict("records")


def test_first_snapshot_creates_panel_observations(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path)
    source_updated_at = fixed_now - timedelta(days=1)

    observations = store.plan_observations([source_record], source_updated_at)
    store.commit(observations, [source_record], source_updated_at)

    assert len(observations) == 1
    rows = _read_rows(store.panel_path)
    assert len(rows) == 1
    assert rows[0]["product_id"] == source_record.product_id
    assert rows[0]["publication_date"] == source_updated_at.date().isoformat()
    with duckdb.connect(str(store.database_path), read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM prices_panel").fetchone()[0] == 1
        schema = {
            row[0]: row[1]
            for row in connection.execute("DESCRIBE prices_panel").fetchall()
        }
    assert schema["publication_date"] == "DATE"
    assert schema["quantity"] == "DECIMAL(7,3)"
    assert schema["standardized_price"] == "DECIMAL(8,5)"
    assert schema["price_aed"] == "DECIMAL(6,3)"
    assert _stata_has_compact_schema(store.panel_path)
    assert not (tmp_path / "state.json").exists()
    assert store.last_source_updated_at() == source_updated_at


def test_each_new_source_version_appends_a_complete_panel_period(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path)
    first_version = fixed_now - timedelta(days=2)
    second_version = fixed_now - timedelta(days=1)
    first = store.plan_observations([source_record], first_version)
    store.commit(first, [source_record], first_version)

    second = store.plan_observations([source_record], second_version)
    store.commit(second, [source_record], second_version)

    assert len(second) == 1
    assert [row["publication_date"] for row in _read_rows(store.panel_path)] == [
        first_version.date().isoformat(),
        second_version.date().isoformat(),
    ]
    assert store.last_source_updated_at() == second_version


def test_panel_period_contains_only_records_present_in_snapshot(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path)
    removed = replace(source_record, product_id="old-product", branch_id="old-branch")
    first_version = fixed_now - timedelta(days=2)
    initial_snapshot = [source_record, removed]
    store.commit(
        store.plan_observations(initial_snapshot, first_version),
        initial_snapshot,
        first_version,
    )

    changed = replace(source_record, price_aed="6.25")
    added = replace(source_record, product_id="new-product", branch_id="new-branch")
    second_version = fixed_now - timedelta(days=1)
    observations = store.plan_observations([changed, added], second_version)
    store.commit(observations, [changed, added], second_version)

    assert {observation.record.key for observation in observations} == {
        source_record.key,
        added.key,
    }
    rows = _read_rows(store.panel_path)
    second_rows = [
        row
        for row in rows
        if row["publication_date"] == second_version.date().isoformat()
    ]
    assert {(row["product_id"], row["branch_id"]) for row in second_rows} == {
        source_record.key,
        added.key,
    }
    assert "change_type" not in rows[0]


def test_writes_stata_panel_with_date_only_schema(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path)
    observations = store.plan_observations([source_record], fixed_now)

    store.commit(observations, [source_record], fixed_now)

    rows = _read_rows(store.panel_path)
    assert list(rows[0]) == PANEL_FIELDS
    assert rows[0]["publication_date"] == "2026-08-11"
    assert "collected_at" not in rows[0]
    assert "source_url" not in rows[0]


def test_failed_commit_is_recoverable(
    tmp_path: Path, source_record: PriceRecord, fixed_now, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = HistoryStore(tmp_path)
    observations = store.plan_observations([source_record], fixed_now)
    original_replace = os.replace
    failures_remaining = 1

    def fail_panel_replace_once(source: str | bytes | Path, destination: str | bytes | Path) -> None:
        nonlocal failures_remaining
        if Path(destination) == store.panel_path and failures_remaining:
            failures_remaining -= 1
            raise OSError("simulated interruption")
        original_replace(source, destination)

    monkeypatch.setattr("core.history.os.replace", fail_panel_replace_once)

    with pytest.raises(OSError, match="simulated interruption"):
        store.commit(observations, [source_record], fixed_now)

    assert store.transaction_path.exists()
    assert store.database_path.exists()
    assert not store.panel_path.exists()

    store.recover_if_needed()

    assert not store.transaction_path.exists()
    assert store.last_source_updated_at() == fixed_now
    assert len(_read_rows(store.panel_path)) == 1


def test_duplicate_snapshot_identity_is_rejected(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path)

    with pytest.raises(ValueError, match="Duplicate snapshot identity"):
        store.plan_observations([source_record, source_record], fixed_now)


def test_rejects_numeric_values_that_exceed_storage_precision(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path)
    unsupported_precision = replace(source_record, standardized_price="8.123456")
    with pytest.raises(HistoryError, match="exceeds DECIMAL"):
        store.commit(
            store.plan_observations([unsupported_precision], fixed_now),
            [unsupported_precision],
            fixed_now,
        )


def test_rejects_dta_only_store_instead_of_migrating_it(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path)
    store.commit(
        store.plan_observations([source_record], fixed_now), [source_record], fixed_now
    )
    store.database_path.unlink()

    with pytest.raises(HistoryError, match="automatic cross-format migration is disabled"):
        store.validate_current_store()
    assert not store.database_path.exists()
    assert store.panel_path.exists()


@pytest.mark.parametrize("legacy_name", ["prices_panel.csv", "prices_history.csv"])
def test_rejects_legacy_csv_without_importing_it(
    tmp_path: Path, legacy_name: str
) -> None:
    store = HistoryStore(tmp_path)
    (tmp_path / legacy_name).write_text("legacy data\n", encoding="utf-8")

    with pytest.raises(HistoryError, match="Legacy CSV data is not imported automatically"):
        store.validate_current_store()
    assert not store.database_path.exists()
    assert not store.panel_path.exists()


def test_rejects_old_duckdb_types_without_rewriting_them(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path)
    store.commit(
        store.plan_observations([source_record], fixed_now), [source_record], fixed_now
    )
    select_fields = ", ".join(
        f'CAST("{field}" AS VARCHAR) AS "{field}"' for field in PANEL_FIELDS
    )
    with duckdb.connect(str(store.database_path)) as connection:
        connection.execute(
            f"CREATE OR REPLACE TABLE prices_panel AS SELECT {select_fields} FROM prices_panel"
        )

    with pytest.raises(HistoryError, match="automatic schema migration is disabled"):
        store.validate_current_store()

    with duckdb.connect(str(store.database_path), read_only=True) as connection:
        assert connection.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'prices_panel' AND column_name = 'publication_date'"
        ).fetchone()[0] == "VARCHAR"


def test_rejects_missing_duckdb_metadata_without_adding_it(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path)
    store.commit(
        store.plan_observations([source_record], fixed_now), [source_record], fixed_now
    )
    with duckdb.connect(str(store.database_path)) as connection:
        connection.execute("DROP TABLE monitor_metadata")

    with pytest.raises(HistoryError, match="monitor_metadata table is required"):
        store.validate_current_store()

    with duckdb.connect(str(store.database_path), read_only=True) as connection:
        assert "monitor_metadata" not in {
            row[0] for row in connection.execute("SHOW TABLES").fetchall()
        }


def test_reads_source_version_from_duckdb_without_state_file(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path)
    store.commit(
        store.plan_observations([source_record], fixed_now), [source_record], fixed_now
    )
    assert store.last_source_updated_at() == fixed_now
    assert not (tmp_path / "state.json").exists()


def test_rejects_stale_local_stata_export_without_repair(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path)
    first_version = fixed_now - timedelta(days=1)
    second_version = fixed_now
    store.commit(
        store.plan_observations([source_record], first_version),
        [source_record],
        first_version,
    )
    store.commit(
        store.plan_observations([source_record], second_version),
        [source_record],
        second_version,
    )
    stale = pd.read_stata(store.panel_path, convert_categoricals=True)
    stale = stale[
        pd.to_datetime(stale["publication_date"]).dt.strftime("%Y-%m-%d")
        == first_version.date().isoformat()
    ]
    stale.to_stata(
        store.panel_path,
        write_index=False,
        version=118,
        convert_dates={"publication_date": "td"},
    )

    with pytest.raises(HistoryError, match="Stata export is out of sync with DuckDB"):
        store.validate_current_store()
    assert len(_read_rows(store.panel_path)) == 1
