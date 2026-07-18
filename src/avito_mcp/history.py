from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock


@dataclass(frozen=True, slots=True)
class HistoryResult:
    new_item_ids: frozenset[str]
    initial_run: bool


class QueryHistory:
    """Persists only searches, run counters, and opaque Avito listing IDs."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = Lock()
        self._initialize()

    def _initialize(self) -> None:
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS searches (
                    id INTEGER PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    query TEXT NOT NULL,
                    max_price_rub INTEGER,
                    search_scope TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_run_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS search_runs (
                    id INTEGER PRIMARY KEY,
                    search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
                    run_at TEXT NOT NULL,
                    eligible_count INTEGER NOT NULL,
                    new_count INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS seen_items (
                    search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
                    item_id TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    PRIMARY KEY (search_id, item_id)
                );
                """
            )

    def record_run(
        self,
        *,
        fingerprint: str,
        query: str,
        max_price_rub: int | None,
        search_scope: str,
        item_ids: list[str],
    ) -> HistoryResult:
        now = datetime.now(UTC).isoformat()
        unique_ids = list(dict.fromkeys(item_ids))
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                row = cursor.execute(
                    "SELECT id FROM searches WHERE fingerprint = ?", (fingerprint,)
                ).fetchone()
                initial_run = row is None
                if row is None:
                    cursor.execute(
                        """
                        INSERT INTO searches (
                            fingerprint, query, max_price_rub, search_scope, created_at, last_run_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (fingerprint, query, max_price_rub, search_scope, now, now),
                    )
                    search_id = int(cursor.lastrowid)
                else:
                    search_id = int(row["id"])
                    cursor.execute(
                        "UPDATE searches SET last_run_at = ? WHERE id = ?", (now, search_id)
                    )

                existing: set[str] = set()
                if unique_ids:
                    placeholders = ",".join("?" for _ in unique_ids)
                    rows = cursor.execute(
                        f"SELECT item_id FROM seen_items WHERE search_id = ? "
                        f"AND item_id IN ({placeholders})",
                        (search_id, *unique_ids),
                    ).fetchall()
                    existing = {str(item["item_id"]) for item in rows}
                new_ids = frozenset(item_id for item_id in unique_ids if item_id not in existing)
                cursor.executemany(
                    """
                    INSERT OR IGNORE INTO seen_items (search_id, item_id, first_seen_at)
                    VALUES (?, ?, ?)
                    """,
                    [(search_id, item_id, now) for item_id in new_ids],
                )
                cursor.execute(
                    """
                    INSERT INTO search_runs (search_id, run_at, eligible_count, new_count)
                    VALUES (?, ?, ?, ?)
                    """,
                    (search_id, now, len(unique_ids), len(new_ids)),
                )
                cursor.execute("COMMIT")
                return HistoryResult(new_item_ids=new_ids, initial_run=initial_run)
            except Exception:
                cursor.execute("ROLLBACK")
                raise
            finally:
                cursor.close()

    def healthcheck(self) -> bool:
        with self._lock:
            return self._connection.execute("SELECT 1").fetchone()[0] == 1

    def summary(self) -> dict[str, int]:
        with self._lock:
            return {
                "searches": int(
                    self._connection.execute("SELECT count(*) FROM searches").fetchone()[0]
                ),
                "runs": int(
                    self._connection.execute("SELECT count(*) FROM search_runs").fetchone()[0]
                ),
                "seen_items": int(
                    self._connection.execute("SELECT count(*) FROM seen_items").fetchone()[0]
                ),
            }

    def close(self) -> None:
        with self._lock:
            self._connection.close()
