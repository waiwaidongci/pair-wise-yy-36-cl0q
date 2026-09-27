from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import (ConflictError, NotFoundError, STATES)


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    outfall TEXT NOT NULL,
                    sampling_time TEXT NOT NULL,
                    instant_value REAL,
                    daily_value REAL,
                    instant_limit REAL,
                    daily_limit REAL,
                    facility_state TEXT NOT NULL
                        CHECK(facility_state IN ('running','shutdown')),
                    calibrated_until TEXT,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    revision INTEGER NOT NULL DEFAULT 1,
                    version INTEGER NOT NULL DEFAULT 1,
                    flags TEXT NOT NULL DEFAULT '[]',
                    registered_by TEXT NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    review_result TEXT,
                    review_note TEXT,
                    closed_by TEXT,
                    closed_at TEXT,
                    external_ref TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(outfall, sampling_time)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_cases_external_ref
                    ON cases(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS case_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('note','rectification','retest')),
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS case_archives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('review','closure')),
                    result TEXT,
                    actor TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    snapshot TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    # ---------- cases ----------

    @staticmethod
    def _case(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["flags"] = json.loads(item["flags"] or "[]")
        return item

    def create_case(self, data: Dict[str, Any], actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO cases(outfall, sampling_time, instant_value, daily_value,
                       instant_limit, daily_limit, facility_state, calibrated_until,
                       status, revision, version, flags, registered_by,
                       external_ref, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (data["outfall"], data["sampling_time"], data["instant_value"],
                     data["daily_value"], data["instant_limit"], data["daily_limit"],
                     data["facility_state"], data["calibrated_until"], data["status"],
                     1, 1, json.dumps(data["flags"], ensure_ascii=False), actor,
                     data.get("external_ref"), now, now),
                )
                case_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            if "external_ref" in str(exc):
                raise ConflictError("external_ref已存在") from exc
            raise ConflictError("同一排放口同一采样时刻的读数已登记，仅保留首条") from exc
        return self.get_case(case_id)

    def get_case(self, case_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise NotFoundError("核查记录不存在")
        return self._case(row)

    def list_cases(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM cases"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._case(row) for row in rows]

    def update_case_decision(self, case_id: int, status: str, expected_version: int,
                             fields: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """带版本号的状态推进（复核/结案）。"""
        now = utc_now()
        sets = ["status=?", "version=version+1", "updated_at=?"]
        params: List[Any] = [status, now]
        for key, value in fields.items():
            sets.append(f"{key}=?")
            params.append(value)
        params.extend([case_id, expected_version])
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"UPDATE cases SET {', '.join(sets)} WHERE id=? AND version=?",
                tuple(params),
            )
            if cur.rowcount == 0:
                if self.conn.execute("SELECT 1 FROM cases WHERE id=?", (case_id,)).fetchone() is None:
                    raise NotFoundError("核查记录不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_case(case_id)

    def apply_correction(self, case_id: int, status: str, expected_version: int,
                         fields: Dict[str, Any], flags: List[str], actor: str) -> Dict[str, Any]:
        """限值/工况更正：revision递增，强制回到待复核，复核/结案资格失效。"""
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE cases SET status=?, revision=revision+1, version=version+1,
                   flags=?, instant_limit=?, daily_limit=?, facility_state=?,
                   reviewed_by=NULL, reviewed_at=NULL, review_result=NULL, review_note=NULL,
                   closed_by=NULL, closed_at=NULL, updated_at=?
                   WHERE id=? AND version=?""",
                (status, json.dumps(flags, ensure_ascii=False),
                 fields["instant_limit"], fields["daily_limit"], fields["facility_state"],
                 now, case_id, expected_version),
            )
            if cur.rowcount == 0:
                if self.conn.execute("SELECT 1 FROM cases WHERE id=?", (case_id,)).fetchone() is None:
                    raise NotFoundError("核查记录不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_case(case_id)

    # ---------- records ----------

    def add_record(self, case_id: int, kind: str, detail: dict, status: str,
                   actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO case_records(case_id, kind, detail, status, created_by, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (case_id, kind, json.dumps(detail, ensure_ascii=False), status, actor, now),
            )
            record_id = int(cur.lastrowid)
        return self.get_record(record_id)

    def get_record(self, record_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM case_records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFoundError("附随记录不存在")
        item = dict(row)
        item["detail"] = json.loads(item["detail"])
        return item

    def list_records(self, case_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM case_records WHERE case_id=? ORDER BY id", (case_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def close_record(self, record_id: int, actor: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE case_records SET status='closed' WHERE id=? AND status='open'",
                (record_id,),
            )
            if cur.rowcount == 0:
                if self.conn.execute("SELECT 1 FROM case_records WHERE id=?", (record_id,)).fetchone() is None:
                    raise NotFoundError("附随记录不存在")
                raise ConflictError("该事项已关闭")
        return self.get_record(record_id)

    def open_rectification_count(self, case_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM case_records WHERE case_id=? AND kind='rectification' AND status='open'",
                (case_id,),
            ).fetchone()
        return int(row["n"])

    def latest_retest(self, case_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM case_records WHERE case_id=? AND kind='retest'
                   ORDER BY id DESC LIMIT 1""",
                (case_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["detail"] = json.loads(item["detail"])
        return item

    # ---------- archives ----------

    def archive(self, case_id: int, kind: str, snapshot: dict, actor: str,
                revision: int, result: Optional[str] = None,
                reason: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO case_archives(case_id, kind, result, actor, revision,
                   snapshot, reason, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (case_id, kind, result, actor, revision,
                 json.dumps(snapshot, ensure_ascii=False, sort_keys=True), reason, now),
            )
            archive_id = int(cur.lastrowid)
        return {"id": archive_id, "case_id": case_id, "kind": kind, "result": result,
                "actor": actor, "revision": revision, "snapshot": snapshot,
                "reason": reason, "created_at": now}

    def list_archives(self, case_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM case_archives WHERE case_id=? ORDER BY id", (case_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item["snapshot"])
            result.append(item)
        return result

    # ---------- audit ----------

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event["id"] = int(cur.lastrowid)
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
