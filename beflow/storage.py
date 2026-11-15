"""Persistent configuration, SQLite transactions and a one-time JSON migration.

Design: one JSON document per Discord guild, stored transactionally in SQLite.
This keeps the V1 schema compatible while isolating each guild and allowing
future migrations to normalized tables. Intended for ONE bot process.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)


class ConfigStore:
    def __init__(self, db_path: str | Path, legacy_path: str | Path | None = None):
        self.db_path = Path(db_path)
        self.legacy_path = Path(legacy_path) if legacy_path else None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(str(self.db_path), timeout=15)
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS guild_config (
                    guild_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            connection.execute("PRAGMA user_version=1")

    def load(self) -> dict:
        with self._connect() as connection:
            rows = connection.execute("SELECT guild_id, payload FROM guild_config").fetchall()
        if rows:
            result = {str(gid): json.loads(payload) for gid, payload in rows}
            if any(not isinstance(value, dict) for value in result.values()):
                raise ValueError("Configuration SQLite invalide : serveur non représenté par un objet")
            return result

        if self.legacy_path and self.legacy_path.is_file():
            # Never erase a corrupt legacy configuration or silently start empty.
            with self.legacy_path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
            if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
                raise ValueError("Configuration JSON V1 invalide")
            if data:
                backup_name = self.legacy_path.name + ".backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                backup_path = self.legacy_path.with_name(backup_name)
                shutil.copy2(self.legacy_path, backup_path)
                self.save(data)
                LOGGER.warning("Configuration V1 migrée vers SQLite ; copie : %s", backup_path)
            return data
        return {}

    def save(self, configuration: dict) -> None:
        if not isinstance(configuration, dict):
            raise TypeError("configuration doit être un dictionnaire")
        now = datetime.now(timezone.utc).isoformat()
        # All rows change in ONE transaction; no partial write on failure.
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM guild_config")
            connection.executemany(
                "INSERT INTO guild_config(guild_id, payload, updated_at) VALUES (?, ?, ?)",
                ((str(gid), json.dumps(data, ensure_ascii=False, separators=(",", ":")), now)
                 for gid, data in configuration.items()),
            )

    def backup(self, destination: str | Path) -> None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as source, sqlite3.connect(str(destination)) as target:
            source.backup(target)
