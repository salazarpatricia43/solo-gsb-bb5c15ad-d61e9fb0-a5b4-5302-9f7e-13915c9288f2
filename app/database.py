"""SQLite persistence layer.

The database is created and migrated on application startup
(see :func:`init_db`). Two tables are used:

* ``recipes``        - one row per named recipe (version group)
* ``recipe_versions`` - immutable saved results, one row per saved version
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATABASE_PATH


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# A single shared connection: SQLite handles locking with a file-level lock
# and our request volume is low.
_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
        _conn = _connect()
    return _conn


def init_db() -> None:
    """Create the data directory and schema if they do not exist."""
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS recipes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT    NOT NULL UNIQUE,
            description   TEXT    NOT NULL DEFAULT '',
            created_at    TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipe_versions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id     INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            version       INTEGER NOT NULL,
            note          TEXT    NOT NULL DEFAULT '',
            request_json  TEXT    NOT NULL,
            result_json   TEXT    NOT NULL,
            created_at    TEXT    NOT NULL,
            UNIQUE (recipe_id, version)
        );

        CREATE INDEX IF NOT EXISTS idx_recipe_versions_recipe_id
            ON recipe_versions(recipe_id);
        """
    )
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Recipes
# --------------------------------------------------------------------------- #
def get_recipe_by_name(name: str) -> sqlite3.Row | None:
    return get_conn().execute(
        "SELECT * FROM recipes WHERE name = ?", (name,)
    ).fetchone()


def get_recipe(recipe_id: int) -> sqlite3.Row | None:
    return get_conn().execute(
        "SELECT * FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()


def list_recipes() -> list[sqlite3.Row]:
    return get_conn().execute(
        "SELECT * FROM recipes ORDER BY created_at DESC, id DESC"
    ).fetchall()


def create_recipe(name: str, description: str) -> sqlite3.Row:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO recipes (name, description, created_at) VALUES (?, ?, ?)",
        (name, description, _now()),
    )
    conn.commit()
    return get_recipe(cur.lastrowid)  # type: ignore[arg-type]


def ensure_recipe(name: str, description: str = "") -> sqlite3.Row:
    """Return the existing recipe with ``name`` or create a new one."""
    existing = get_recipe_by_name(name)
    if existing is not None:
        return existing
    return create_recipe(name, description)


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #
def list_versions(recipe_id: int) -> list[sqlite3.Row]:
    return get_conn().execute(
        "SELECT * FROM recipe_versions WHERE recipe_id = ? ORDER BY version DESC",
        (recipe_id,),
    ).fetchall()


def get_version(recipe_id: int, version: int) -> sqlite3.Row | None:
    return get_conn().execute(
        "SELECT * FROM recipe_versions WHERE recipe_id = ? AND version = ?",
        (recipe_id, version),
    ).fetchone()


def latest_version_number(recipe_id: int) -> int:
    row = get_conn().execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM recipe_versions WHERE recipe_id = ?",
        (recipe_id,),
    ).fetchone()
    return int(row["v"])


def create_version(
    recipe_id: int,
    note: str,
    request_payload: dict[str, Any],
    result_payload: dict[str, Any],
) -> sqlite3.Row:
    conn = get_conn()
    next_version = latest_version_number(recipe_id) + 1
    cur = conn.execute(
        """
        INSERT INTO recipe_versions
            (recipe_id, version, note, request_json, result_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            recipe_id,
            next_version,
            note,
            json.dumps(request_payload, ensure_ascii=False),
            json.dumps(result_payload, ensure_ascii=False),
            _now(),
        ),
    )
    conn.commit()
    row = get_version(recipe_id, next_version)
    assert row is not None
    return row


def serialize_version(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "recipe_id": row["recipe_id"],
        "version": row["version"],
        "note": row["note"],
        "created_at": row["created_at"],
        "request": json.loads(row["request_json"]),
        "result": json.loads(row["result_json"]),
    }


def serialize_recipe(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "created_at": row["created_at"],
        "latest_version": latest_version_number(row["id"]),
    }
