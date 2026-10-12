from __future__ import annotations

import csv
from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path

import pytest
import pandas as pd
import duckdb

from core.client import PriceRecord
from core.history import LEGACY_EVENT_FIELDS, PANEL_FIELDS, HistoryStore


SOURCE_URL = "https://www.moet.gov.ae/en/essential-goods-prices-platform"


def _read_rows(path: Path) -> list[dict[str, str]]:
    frame = pd.read_stata(path, convert_categoricals=False).fillna("").astype(str)
    return frame.to_dict("records")


def test_first_snapshot_creates_panel_observations(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path, SOURCE_URL)
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
    state = json.loads(store.state_path.read_text(encoding="utf-8"))
    assert state["record_count"] == 1
    assert len(state["fingerprints"]) == 1
    assert store.last_source_updated_at() == source_updated_at


def test_each_new_source_version_appends_a_complete_panel_period(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path, SOURCE_URL)
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
    store = HistoryStore(tmp_path, SOURCE_URL)
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
    store = HistoryStore(tmp_path, SOURCE_URL)
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
    store = HistoryStore(tmp_path, SOURCE_URL)
    observations = store.plan_observations([source_record], fixed_now)
    original_replace = os.replace
    failures_remaining = 1

    def fail_state_replace_once(source: str | bytes | Path, destination: str | bytes | Path) -> None:
        nonlocal failures_remaining
        if Path(destination) == store.state_path and failures_remaining:
            failures_remaining -= 1
            raise OSError("simulated interruption")
        original_replace(source, destination)

    monkeypatch.setattr("core.history.os.replace", fail_state_replace_once)

    with pytest.raises(OSError, match="simulated interruption"):
        store.commit(observations, [source_record], fixed_now)

    assert store.transaction_path.exists()
    assert store.panel_path.exists()
    assert not store.state_path.exists()

    store.recover_if_needed()

    assert not store.transaction_path.exists()
    assert store.last_source_updated_at() == fixed_now
    assert len(_read_rows(store.panel_path)) == 1


def test_duplicate_snapshot_identity_is_rejected(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path, SOURCE_URL)

    with pytest.raises(ValueError, match="Duplicate snapshot identity"):
        store.plan_observations([source_record, source_record], fixed_now)


def test_migrates_legacy_events_into_complete_panel_periods(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path, SOURCE_URL)
    removed = replace(source_record, product_id="old-product", branch_id="old-branch")
    changed = replace(source_record, price_aed="6.25")
    added = replace(source_record, product_id="new-product", branch_id="new-branch")
    first_version = fixed_now - timedelta(days=2)
    second_version = fixed_now - timedelta(days=1)

    final_snapshot = [changed, added]
    store.commit(
        store.plan_observations(final_snapshot, second_version),
        final_snapshot,
        second_version,
    )
    store.panel_path.unlink()
    store.database_path.unlink()

    legacy_rows = [
        ("initial", first_version, source_record),
        ("initial", first_version, removed),
        ("changed", second_version, changed),
        ("new", second_version, added),
        ("removed", second_version, removed),
    ]
    with store.legacy_event_path.open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=LEGACY_EVENT_FIELDS)
        writer.writeheader()
        for change_type, version, record in legacy_rows:
            writer.writerow(
                {
                    "change_type": change_type,
                    "source_updated_at": version.isoformat(timespec="seconds"),
                    "collected_at": fixed_now.isoformat(timespec="seconds"),
                    "source_url": SOURCE_URL,
                    **record.as_dict(),
                }
            )

    migrated = store.migrate_existing_data_if_needed()

    rows = _read_rows(store.panel_path)
    assert migrated == 4
    assert len(rows) == 4
    with duckdb.connect(str(store.database_path), read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM prices_panel").fetchone()[0] == 4
    assert store.legacy_event_path.exists()
    second_rows = [
        row
        for row in rows
        if row["publication_date"] == second_version.date().isoformat()
    ]
    assert {(row["product_id"], row["branch_id"]) for row in second_rows} == {
        changed.key,
        added.key,
    }
    assert {row["price_aed"] for row in second_rows if row["product_id"] == changed.product_id} == {
        "6.25"
    }


def test_migrates_existing_local_stata_panel_to_duckdb(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path, SOURCE_URL)
    store.commit(
        store.plan_observations([source_record], fixed_now), [source_record], fixed_now
    )
    store.database_path.unlink()

    migrated = store.migrate_existing_data_if_needed()

    assert migrated == 1
    with duckdb.connect(str(store.database_path), read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM prices_panel").fetchone()[0] == 1


def test_rebuilds_missing_local_state_from_duckdb(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path, SOURCE_URL)
    store.commit(
        store.plan_observations([source_record], fixed_now), [source_record], fixed_now
    )
    store.state_path.unlink()
    restored_store = HistoryStore(tmp_path, SOURCE_URL)

    assert restored_store.last_source_updated_at() == fixed_now
    state = json.loads(restored_store.state_path.read_text(encoding="utf-8"))
    assert state["record_count"] == 1
    assert next(iter(state["records"].values()))["product_id"] == source_record.product_id


def test_refreshes_stale_local_stata_export_from_duckdb(
    tmp_path: Path, source_record: PriceRecord, fixed_now
) -> None:
    store = HistoryStore(tmp_path, SOURCE_URL)
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
    stale = pd.read_stata(store.panel_path, convert_categoricals=False)
    stale = stale[stale["publication_date"] == first_version.date().isoformat()]
    stale.to_stata(store.panel_path, write_index=False, version=118)

    migrated = store.migrate_existing_data_if_needed()

    assert migrated == 2
    assert len(_read_rows(store.panel_path)) == 2
