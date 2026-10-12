from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import duckdb

from core.client import PriceRecord, SourceDataError
from core.history import HistoryStore
from core.service import PriceMonitor


class FakeClient:
    def __init__(
        self,
        source_updated_at: datetime,
        snapshot: list[PriceRecord] | Exception,
    ) -> None:
        self.source_updated_at = source_updated_at
        self.snapshot = snapshot
        self.version_calls = 0
        self.snapshot_calls = 0
        self.settings = SimpleNamespace(
            source_url="https://www.moet.gov.ae/en/essential-goods-prices-platform"
        )

    def fetch_source_updated_at(self) -> datetime:
        self.version_calls += 1
        return self.source_updated_at

    def fetch_snapshot(self) -> list[PriceRecord]:
        self.snapshot_calls += 1
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        return self.snapshot


def test_unchanged_version_skips_snapshot_and_panel_write(
    tmp_path: Path, source_record: PriceRecord, fixed_now: datetime
) -> None:
    history = HistoryStore(tmp_path)
    initial = history.plan_observations([source_record], fixed_now)
    _commit(history, initial, [source_record], fixed_now)
    panel_before = history.panel_path.read_bytes()
    client = FakeClient(fixed_now, AssertionError("full snapshot must not be called"))
    monitor = PriceMonitor(client, history)

    result = monitor.run_once()

    assert result.update_found is False
    assert result.fetched_count == 0
    assert result.appended_count == 0
    assert client.snapshot_calls == 0
    assert history.panel_path.read_bytes() == panel_before


def test_same_publication_date_is_collected_only_once(
    tmp_path: Path, source_record: PriceRecord, fixed_now: datetime
) -> None:
    history = HistoryStore(tmp_path)
    initial = history.plan_observations([source_record], fixed_now)
    _commit(history, initial, [source_record], fixed_now)
    panel_before = history.panel_path.read_bytes()
    later_same_date = fixed_now + timedelta(hours=8)
    client = FakeClient(
        later_same_date, AssertionError("same publication date must not be downloaded")
    )

    result = PriceMonitor(client, history).run_once()

    assert result.update_found is False
    assert client.snapshot_calls == 0
    assert history.panel_path.read_bytes() == panel_before


def test_changed_version_fetches_and_commits_complete_snapshot(
    tmp_path: Path, source_record: PriceRecord, fixed_now: datetime
) -> None:
    source_version = fixed_now - timedelta(hours=1)
    history = HistoryStore(tmp_path)
    client = FakeClient(source_version, [source_record])
    monitor = PriceMonitor(client, history)

    result = monitor.run_once()

    assert result.update_found is True
    assert result.fetched_count == 1
    assert result.appended_count == 1
    assert client.snapshot_calls == 1
    assert history.last_source_updated_at() == source_version
    with duckdb.connect(str(history.database_path), read_only=True) as connection:
        provenance = connection.execute(
            "SELECT publication_date, source_updated_at, source_url "
            "FROM collection_metadata"
        ).fetchone()
    assert str(provenance[0]) == source_version.date().isoformat()
    assert provenance[1] == source_version.isoformat(timespec="seconds")
    assert provenance[2] == client.settings.source_url


def test_failure_returns_without_committing_new_version(
    tmp_path: Path, source_record: PriceRecord, fixed_now: datetime
) -> None:
    old_version = fixed_now - timedelta(days=2)
    new_version = fixed_now - timedelta(days=1)
    history = HistoryStore(tmp_path)
    initial = history.plan_observations([source_record], old_version)
    _commit(history, initial, [source_record], old_version)
    panel_before = history.panel_path.read_bytes()
    client = FakeClient(new_version, SourceDataError("source unavailable"))
    monitor = PriceMonitor(client, history)

    with pytest.raises(SourceDataError, match="source unavailable"):
        monitor.run_once()

    assert history.last_source_updated_at() == old_version
    assert history.panel_path.read_bytes() == panel_before


def _commit(history, observations, snapshot, source_updated_at) -> None:
    history.commit(
        observations,
        snapshot,
        source_updated_at,
        collected_at=source_updated_at,
        source_url="https://www.moet.gov.ae/en/essential-goods-prices-platform",
    )
