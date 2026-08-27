import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional


class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._ensure_db_exists()

    def _ensure_db_exists(self):
        db_file = Path(self.db_path)
        if not db_file.parent.exists():
            db_file.parent.mkdir(parents=True, exist_ok=True)

    @property
    def connection(self) -> sqlite3.Connection:
        if not hasattr(self._local, "connection"):
            self._local.connection = sqlite3.connect(self.db_path)
            self._local.connection.row_factory = sqlite3.Row
        return self._local.connection

    def close(self):
        if hasattr(self._local, "connection"):
            self._local.connection.close()
            delattr(self._local, "connection")

    def execute(self, sql: str, params: tuple = ()) -> bool:
        try:
            cursor = self.connection.cursor()
            cursor.execute(sql, params)
            self.connection.commit()
            return True
        except Exception as e:
            print(f"[ARCPvPKD] Execute SQL error: {e}")
            self.connection.rollback()
            return False

    def query_one(self, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        try:
            cursor = self.connection.cursor()
            cursor.execute(sql, params)
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            print(f"[ARCPvPKD] Query one error: {e}")
            return None

    def query_all(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        try:
            cursor = self.connection.cursor()
            cursor.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            print(f"[ARCPvPKD] Query all error: {e}")
            return []

    def create_table(self, table: str, fields: Dict[str, str]) -> bool:
        field_defs = ",".join([f"{k} {v}" for k, v in fields.items()])
        return self.execute(f"CREATE TABLE IF NOT EXISTS {table} ({field_defs})")
