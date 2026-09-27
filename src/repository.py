from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import STATUSES
# 登记后允许更正的字段（限值或工况等）；更正即触发复核/结案资格失效
CORRECTABLE = (
    "outfall", "sampled_at", "instant_concentration", "daily_avg_concentration",
    "permit_limit_instant", "permit_limit_daily", "facility_status",
    "calibrated_at", "note",
)


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
        self._migrate()

    def _migrate(self) -> None:
        """对早于records.sampled_at的旧库做就地补列。"""
        with self._lock, self.conn:
            cols = {r["name"] for r in self.conn.execute(
                "PRAGMA table_info(records)").fetchall()}
            if cols and "sampled_at" not in cols:
                self.conn.execute("ALTER TABLE records ADD COLUMN sampled_at TEXT")

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s + "'" for s in STATUSES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS emission_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    outfall TEXT NOT NULL,
                    sampled_at TEXT NOT NULL,
                    instant_concentration REAL NOT NULL,
                    daily_avg_concentration REAL,
                    permit_limit_instant REAL NOT NULL,
                    permit_limit_daily REAL NOT NULL,
                    facility_status TEXT NOT NULL
                        CHECK(facility_status IN ('running','stopped')),
                    calibrated_at TEXT,
                    note TEXT,
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_corrected_by TEXT,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    review_conclusion TEXT,
                    review_note TEXT,
                    closed_by TEXT,
                    closed_at TEXT,
                    close_note TEXT,
                    UNIQUE(outfall, sampled_at)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_checks_external_ref
                    ON emission_checks(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    check_id INTEGER NOT NULL
                        REFERENCES emission_checks(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL
                        CHECK(kind IN ('retest','rectification','evidence')),
                    detail TEXT NOT NULL,
                    result TEXT CHECK(result IS NULL OR result IN ('pass','fail')),
                    sampled_at TEXT,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(check_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS conclusion_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    check_id INTEGER NOT NULL
                        REFERENCES emission_checks(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('review','closure')),
                    conclusion TEXT,
                    note TEXT,
                    actor TEXT NOT NULL,
                    concluded_at TEXT NOT NULL,
                    invalidated_reason TEXT NOT NULL,
                    invalidated_by TEXT NOT NULL,
                    invalidated_at TEXT NOT NULL
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

    # ---------- 核查登记 ----------
    def create_check(self, data: Dict[str, Any], actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO emission_checks(
                           outfall, sampled_at, instant_concentration,
                           daily_avg_concentration, permit_limit_instant,
                           permit_limit_daily, facility_status, calibrated_at, note,
                           severity, status, version, external_ref,
                           created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (data["outfall"], data["sampled_at"],
                     data["instant_concentration"], data["daily_avg_concentration"],
                     data["permit_limit_instant"], data["permit_limit_daily"],
                     data["facility_status"], data["calibrated_at"], data["note"],
                     data["severity"], data["status"], 1, data["external_ref"],
                     actor, now, now),
                )
                check_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("同一排放口同一采样时刻只保留首条登记") from exc
        return self.get_check(check_id)

    def get_check(self, check_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM emission_checks WHERE id=?", (check_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("核查记录不存在")
        return dict(row)

    def list_checks(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM emission_checks"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY sampled_at DESC, id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # ---------- 更正：限值/工况等更正后资格立即失效 ----------
    def correct_check(self, check_id: int, fields: Dict[str, Any],
                      expected_version: int, new_status: str,
                      severity: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        updates = {k: v for k, v in fields.items() if k in CORRECTABLE}
        assignments = ", ".join(f"{k}=?" for k in updates)
        params: List[Any] = list(updates.values())
        params += [new_status, severity, actor, now, check_id, expected_version]
        sql = f"""UPDATE emission_checks SET {assignments},
                    status=?, severity=?, last_corrected_by=?,
                    reviewed_by=NULL, reviewed_at=NULL, review_conclusion=NULL,
                    review_note=NULL, closed_by=NULL, closed_at=NULL, close_note=NULL,
                    updated_at=?, version=version+1
                  WHERE id=? AND version=?"""
        with self._lock, self.conn:
            try:
                cur = self.conn.execute(sql, params)
            except sqlite3.IntegrityError as exc:
                raise ConflictError("同一排放口同一采样时刻只保留首条登记") from exc
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM emission_checks WHERE id=?", (check_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError("核查记录不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_check(check_id)

    # ---------- 复核 / 结案 ----------
    def mark_reviewed(self, check_id: int, expected_version: int, conclusion: str,
                      note: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE emission_checks
                   SET status='confirmed', review_conclusion=?, review_note=?,
                       reviewed_by=?, reviewed_at=?, updated_at=?, version=version+1
                   WHERE id=? AND version=? AND status='pending_review'""",
                (conclusion, note, actor, now, now, check_id, expected_version),
            )
            if cur.rowcount == 0:
                self._guard_version(check_id, expected_version)
                raise ConflictError("该记录当前不在待复核状态")
        return self.get_check(check_id)

    def mark_closed(self, check_id: int, expected_version: int,
                    note: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE emission_checks
                   SET status='closed', closed_by=?, closed_at=?, close_note=?,
                       updated_at=?, version=version+1
                   WHERE id=? AND version=? AND status='confirmed'""",
                (actor, now, note, now, check_id, expected_version),
            )
            if cur.rowcount == 0:
                self._guard_version(check_id, expected_version)
                raise ConflictError("只有已复核确认的记录才能结案")
        return self.get_check(check_id)

    def _guard_version(self, check_id: int, expected_version: int) -> None:
        row = self.conn.execute(
            "SELECT version FROM emission_checks WHERE id=?", (check_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("核查记录不存在")
        if row["version"] != expected_version:
            raise ConflictError("版本冲突，请刷新后重试")

    # ---------- 复测 / 整改 / 佐证 ----------
    def add_record(self, check_id: int, kind: str, detail: str,
                   result: Optional[str], sampled_at: Optional[str], status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_check(check_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(check_id, kind, detail, result, sampled_at,
                           status, external_ref, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (check_id, kind, detail, result, sampled_at, status,
                     external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?",
                                    (record_id,)).fetchone()
        return dict(row)

    def close_rectification(self, check_id: int, record_id: int) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE records SET status='closed'
                   WHERE id=? AND check_id=? AND kind='rectification'
                     AND status='open'""",
                (record_id, check_id),
            )
            if cur.rowcount == 0:
                row = self.conn.execute(
                    "SELECT * FROM records WHERE id=? AND check_id=?",
                    (record_id, check_id),
                ).fetchone()
                if row is None:
                    raise NotFoundError("整改事项不存在")
                raise ConflictError("该事项不是未关闭的整改事项")
            row = self.conn.execute("SELECT * FROM records WHERE id=?",
                                    (record_id,)).fetchone()
        return dict(row)

    def list_records(self, check_id: int) -> List[Dict[str, Any]]:
        self.get_check(check_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE check_id=? ORDER BY id", (check_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_rectification_count(self, check_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                """SELECT COUNT(*) AS n FROM records
                   WHERE check_id=? AND kind='rectification' AND status='open'""",
                (check_id,),
            ).fetchone()
        return int(row["n"])

    def has_passing_retest_since(self, check_id: int, reviewed_at: str) -> bool:
        """是否存在复核合格之后复测（以复测采样时刻为准）达标的记录。"""
        with self._lock:
            row = self.conn.execute(
                """SELECT 1 FROM records
                   WHERE check_id=? AND kind='retest' AND result='pass'
                     AND sampled_at IS NOT NULL AND sampled_at>=? LIMIT 1""",
                (check_id, reviewed_at),
            ).fetchone()
        return row is not None

    def followup_summary(self) -> Dict[int, Dict[str, Any]]:
        """列表页一次取全：未关闭整改数、复核后最近一次达标复测的采样时刻。"""
        with self._lock:
            rect_rows = self.conn.execute(
                """SELECT check_id, COUNT(*) AS n FROM records
                   WHERE kind='rectification' AND status='open'
                   GROUP BY check_id"""
            ).fetchall()
            retest_rows = self.conn.execute(
                """SELECT check_id, MAX(sampled_at) AS last_pass FROM records
                   WHERE kind='retest' AND result='pass' AND sampled_at IS NOT NULL
                   GROUP BY check_id"""
            ).fetchall()
        summary: Dict[int, Dict[str, Any]] = {}
        for row in rect_rows:
            summary.setdefault(row["check_id"], {})["open_rectifications"] = int(row["n"])
        for row in retest_rows:
            summary.setdefault(row["check_id"], {})["last_passing_retest"] = row["last_pass"]
        return summary

    # ---------- 旧结论留档 ----------
    def archive_conclusion(self, check_id: int, kind: str, conclusion: Optional[str],
                           note: Optional[str], actor: str, concluded_at: str,
                           reason: str, invalidated_by: str) -> int:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO conclusion_history(
                       check_id, kind, conclusion, note, actor, concluded_at,
                       invalidated_reason, invalidated_by, invalidated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (check_id, kind, conclusion, note, actor, concluded_at,
                 reason, invalidated_by, now),
            )
            return int(cur.lastrowid)

    def list_history(self, check_id: int) -> List[Dict[str, Any]]:
        self.get_check(check_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM conclusion_history WHERE check_id=? ORDER BY id",
                (check_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 审计链 ----------
    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor,
                   detail, previous_hash, entry_hash, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"],
                 event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
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
            rows = self.conn.execute(
                "SELECT * FROM audit_events ORDER BY id").fetchall()
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
