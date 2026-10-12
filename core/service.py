"""One-run application orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging

from .client import MoetClient
from .history import HistoryStore


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunResult:
    """Observable result of one scheduled or manual run."""

    source_updated_at: datetime
    update_found: bool
    fetched_count: int
    appended_count: int


class PriceMonitor:
    """Coordinate version checking, snapshot collection, and history commit."""

    def __init__(
        self,
        client: MoetClient,
        history: HistoryStore,
    ) -> None:
        self.client = client
        self.history = history

    def run_once(self) -> RunResult:
        LOGGER.info("Starting MOET essential-goods price check")
        self.history.recover_if_needed()
        migrated_count = self.history.migrate_existing_data_if_needed()
        if migrated_count:
            LOGGER.info(
                "Migrated existing data across panel formats: %s observations",
                migrated_count,
            )
        last_version = self.history.last_source_updated_at()
        source_version = self.client.fetch_source_updated_at()

        if last_version is not None and source_version.date() == last_version.date():
            LOGGER.info(
                "No update: publication date remains %s",
                source_version.date().isoformat(),
            )
            return RunResult(
                source_updated_at=source_version,
                update_found=False,
                fetched_count=0,
                appended_count=0,
            )

        LOGGER.info(
            "Source update detected: previous=%s current=%s",
            last_version.isoformat() if last_version else "none",
            source_version.isoformat(),
        )
        snapshot = self.client.fetch_snapshot()
        observations = self.history.plan_observations(snapshot, source_version)
        self.history.commit(observations, snapshot, source_version)
        LOGGER.info(
            "Committed source version %s: fetched=%s appended=%s",
            source_version.isoformat(),
            len(snapshot),
            len(observations),
        )
        return RunResult(
            source_updated_at=source_version,
            update_found=True,
            fetched_count=len(snapshot),
            appended_count=len(observations),
        )
