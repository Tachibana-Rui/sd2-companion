"""Durable, immutable report submissions and bounded retry scheduling."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .upload import ACCEPTED_STATUSES, MAX_RESPONSE_BYTES, payload_bytes, safe_receipt


_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid_outbox_time")
    return float(value)


def _error_code(value):
    if not isinstance(value, str) or not _ERROR_CODE.fullmatch(value):
        raise ValueError("invalid_error_code")
    return value


def _capture_fingerprint(payload):
    from .local_replay_library import report_fingerprint
    try:
        return report_fingerprint(payload)
    except (KeyError, TypeError, ValueError):
        return None


class Outbox:
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS companion_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS companion_outbox (
                submission_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending', 'sent', 'rejected')),
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                next_attempt_at REAL NOT NULL,
                last_error TEXT,
                receipt TEXT
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS companion_outbox_due ON companion_outbox(state, next_attempt_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS companion_delivery_state (
                submission_id TEXT PRIMARY KEY, report_fingerprint TEXT,
                server_status TEXT, pending_reason TEXT,
                next_receipt_check_at REAL NOT NULL DEFAULT 0)""")
            db.execute("""CREATE TABLE IF NOT EXISTS companion_deleted_reports (
                fingerprint TEXT PRIMARY KEY, deleted_at REAL NOT NULL)""")
            # Add metadata alongside the immutable, backwards compatible queue.
            for row in db.execute("""SELECT o.submission_id,o.payload,o.receipt,o.state
                FROM companion_outbox o LEFT JOIN companion_delivery_state d
                ON d.submission_id=o.submission_id WHERE d.submission_id IS NULL"""):
                try:
                    capture = _capture_fingerprint(json.loads(row["payload"]))
                except (ValueError, TypeError):
                    capture = None
                try:
                    receipt = json.loads(row["receipt"]) if row["receipt"] else {}
                    if not isinstance(receipt, dict):
                        receipt = {}
                except (ValueError, TypeError):
                    receipt = {}
                state = receipt.get("status") if receipt.get("status") in ("accepted", "pending_match") else None
                if row["state"] == "sent" and state is None:
                    state = "received"
                db.execute("""INSERT OR IGNORE INTO companion_delivery_state
                    (submission_id,report_fingerprint,server_status) VALUES(?,?,?)""",
                           (row["submission_id"], capture, state))
            db.execute("INSERT OR IGNORE INTO companion_meta(key, value) VALUES('installation_id', ?)",
                       (str(uuid.uuid4()),))
            self.installation_id = db.execute("SELECT value FROM companion_meta WHERE key='installation_id'").fetchone()[0]
        try:
            uuid.UUID(self.installation_id)
        except (TypeError, ValueError, AttributeError):
            raise ValueError("invalid_installation_id") from None

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, payload):
        encoded = payload_bytes(payload)
        submission_id = payload["submission_id"]
        fingerprint = hashlib.sha256(encoded).hexdigest()
        now = _timestamp(self._clock())
        capture = _capture_fingerprint(payload)
        if capture:
            from .local_deletion import deleted_fingerprints
            if capture in deleted_fingerprints(self.path.parent):
                return False
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if capture and db.execute("SELECT 1 FROM companion_deleted_reports WHERE fingerprint=?", (capture,)).fetchone():
                return False
            previous = db.execute("SELECT payload_sha256 FROM companion_outbox WHERE submission_id=?", (submission_id,)).fetchone()
            if previous is not None:
                if previous["payload_sha256"] != fingerprint:
                    raise ValueError("submission_id_content_conflict")
                return False
            db.execute("""INSERT INTO companion_outbox
                (submission_id,payload,payload_sha256,state,created_at,updated_at,next_attempt_at)
                VALUES(?,?,?,'pending',?,?,?)""",
                       (submission_id, encoded.decode("utf-8"), fingerprint, now, now, now))
            db.execute("INSERT OR IGNORE INTO companion_delivery_state(submission_id,report_fingerprint) VALUES(?,?)",
                       (submission_id, capture))
        return True

    def due(self, now=None, limit=10):
        now = _timestamp(self._clock() if now is None else now)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("invalid_outbox_limit")
        with self._connection() as db:
            rows = db.execute("""SELECT submission_id,payload,payload_sha256,attempts,next_attempt_at
                FROM companion_outbox WHERE state='pending' AND next_attempt_at<=?
                ORDER BY next_attempt_at,created_at,submission_id LIMIT ?""", (now, limit)).fetchall()
        result = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
                encoded = payload_bytes(payload)
            except (ValueError, TypeError, RecursionError):
                raise ValueError("invalid_outbox_payload") from None
            if (payload["submission_id"] != row["submission_id"]
                    or hashlib.sha256(encoded).hexdigest() != row["payload_sha256"]):
                raise ValueError("invalid_outbox_payload")
            result.append({"id": row["submission_id"], "submission_id": row["submission_id"],
                           "payload": payload, "attempts": row["attempts"],
                           "next_attempt_at": row["next_attempt_at"]})
        return result

    def is_pending(self, submission_id):
        with self._connection() as db:
            return db.execute("""SELECT 1 FROM companion_outbox o
                LEFT JOIN companion_delivery_state d ON d.submission_id=o.submission_id
                WHERE o.submission_id=? AND o.state='pending' AND NOT EXISTS
                    (SELECT 1 FROM companion_deleted_reports x WHERE x.fingerprint=d.report_fingerprint)""",
                (submission_id,)).fetchone() is not None

    def mark_sent(self, job_id, receipt):
        receipt = safe_receipt(receipt, job_id)
        encoded = json.dumps(receipt, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ValueError("invalid_receipt")
        with self._connection() as db:
            cursor = db.execute("""UPDATE companion_outbox SET state='sent',receipt=?,last_error=NULL,
                attempts=attempts+1,updated_at=? WHERE submission_id=? AND state='pending'""",
                                (encoded, _timestamp(self._clock()), job_id))
            changed = cursor.rowcount == 1
            if changed:
                state = receipt["status"] if receipt["status"] in ("accepted", "pending_match") else "received"
                db.execute("""UPDATE companion_delivery_state SET server_status=?,pending_reason=?,
                    next_receipt_check_at=0 WHERE submission_id=?""",
                           (state, receipt.get("pending_reason"), job_id))
            return changed

    def cancel_report(self, fingerprint):
        """Cancel this local capture durably; already delivered reports stay remote."""
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("invalid_report_fingerprint")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO companion_deleted_reports VALUES(?,?)",
                       (fingerprint, _timestamp(self._clock())))
            removed = db.execute("""DELETE FROM companion_outbox WHERE state IN ('pending','rejected')
                AND submission_id IN (SELECT submission_id FROM companion_delivery_state WHERE report_fingerprint=?)""",
                                 (fingerprint,))
            return removed.rowcount

    def receipts_due(self, *, now=None, limit=5):
        now = _timestamp(self._clock() if now is None else now)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_outbox_limit")
        with self._connection() as db:
            rows = db.execute("""SELECT d.submission_id FROM companion_delivery_state d
                JOIN companion_outbox o ON o.submission_id=d.submission_id
                WHERE o.state='sent' AND d.server_status IN ('pending_match','received')
                AND d.next_receipt_check_at<=? AND NOT EXISTS
                    (SELECT 1 FROM companion_deleted_reports x WHERE x.fingerprint=d.report_fingerprint)
                ORDER BY d.next_receipt_check_at,o.created_at LIMIT ?""", (now, limit)).fetchall()
            ids = [row[0] for row in rows]
            db.executemany("UPDATE companion_delivery_state SET next_receipt_check_at=? WHERE submission_id=?",
                           ((now + 60, identifier) for identifier in ids))
            return ids

    def update_receipt(self, job_id, receipt):
        receipt = safe_receipt(receipt, job_id)
        if receipt["status"] not in ("accepted", "pending_match"):
            raise ValueError("invalid_receipt")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT server_status FROM companion_delivery_state WHERE submission_id=?", (job_id,)).fetchone()
            if previous is not None and previous[0] == "accepted" and receipt["status"] != "accepted":
                return False
            cursor = db.execute("UPDATE companion_outbox SET receipt=?,updated_at=? WHERE submission_id=? AND state='sent'",
                (json.dumps(receipt, separators=(",", ":")), _timestamp(self._clock()), job_id))
            if cursor.rowcount:
                # Once verified, transient old responses cannot regress a match.
                db.execute("""UPDATE companion_delivery_state SET server_status=?,pending_reason=?
                    WHERE submission_id=? AND COALESCE(server_status,'')!='accepted'""",
                           (receipt["status"], receipt.get("pending_reason"), job_id))
            return cursor.rowcount == 1

    def report_statuses(self):
        result = {}
        priority = {"received": 0, "rejected": 1, "queued": 2, "pending_match": 3, "accepted": 4}
        with self._connection() as db:
            for row in db.execute("""SELECT d.report_fingerprint,d.server_status,d.pending_reason,o.state
                FROM companion_delivery_state d JOIN companion_outbox o ON o.submission_id=d.submission_id
                WHERE d.report_fingerprint IS NOT NULL AND NOT EXISTS
                    (SELECT 1 FROM companion_deleted_reports x WHERE x.fingerprint=d.report_fingerprint)"""):
                state = ("queued" if row["state"] == "pending" else "rejected" if row["state"] == "rejected"
                         else row["server_status"] or "received")
                old = result.get(row["report_fingerprint"], {}).get("server_status", "received")
                if row["report_fingerprint"] not in result or priority.get(state, 0) >= priority.get(old, 0):
                    result[row["report_fingerprint"]] = {"server_status": state, "pending_reason": row["pending_reason"]}
        return result

    def mark_retry(self, job_id, error_code, *, now=None, retry_after=None):
        error_code = _error_code(error_code)
        now = _timestamp(self._clock() if now is None else now)
        if retry_after is not None:
            retry_after = min(86400.0, _timestamp(retry_after))
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT attempts FROM companion_outbox WHERE submission_id=? AND state='pending'", (job_id,)).fetchone()
            if row is None:
                return False
            backoff = min(3600.0, 5.0 * 2 ** min(row["attempts"], 10))
            delay = max(backoff, retry_after if retry_after is not None else 0)
            db.execute("""UPDATE companion_outbox SET attempts=attempts+1,updated_at=?,
                next_attempt_at=?,last_error=? WHERE submission_id=? AND state='pending'""",
                       (now, now + delay, error_code, job_id))
        return True

    def mark_rejected(self, job_id, error_code):
        error_code = _error_code(error_code)
        with self._connection() as db:
            cursor = db.execute("""UPDATE companion_outbox SET state='rejected',last_error=?,
                attempts=attempts+1,updated_at=? WHERE submission_id=? AND state='pending'""",
                                (error_code, _timestamp(self._clock()), job_id))
            return cursor.rowcount == 1

    def retry_rejected(self, *, limit=100, now=None):
        """Explicitly retry retained refusals after repairing token or URL.

        Report identity/content and the previous attempt count are unchanged.
        Already acknowledged reports are never requeued by this operation.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("invalid_outbox_limit")
        now = _timestamp(self._clock() if now is None else now)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("""UPDATE companion_outbox
                SET state='pending',next_attempt_at=?,updated_at=?,last_error=NULL
                WHERE submission_id IN (
                    SELECT submission_id FROM companion_outbox WHERE state='rejected'
                    ORDER BY updated_at,created_at,submission_id LIMIT ?
                ) AND state='rejected'""", (now, now, limit))
            return cursor.rowcount

    def status(self):
        now = _timestamp(self._clock())
        result = {"installation_id": self.installation_id, "pending": 0, "sent": 0, "rejected": 0,
                  "due": 0, "next_attempt_at": None}
        with self._connection() as db:
            eligible = """FROM companion_outbox o LEFT JOIN companion_delivery_state d ON d.submission_id=o.submission_id
                WHERE NOT EXISTS (SELECT 1 FROM companion_deleted_reports x WHERE x.fingerprint=d.report_fingerprint)"""
            for row in db.execute("SELECT o.state,COUNT(*) AS count " + eligible + " GROUP BY o.state"):
                result[row["state"]] = row["count"]
            result["due"] = db.execute("SELECT COUNT(*) FROM companion_outbox WHERE state='pending' AND next_attempt_at<=?", (now,)).fetchone()[0]
            result["next_attempt_at"] = db.execute("SELECT MIN(next_attempt_at) FROM companion_outbox WHERE state='pending'").fetchone()[0]
            for column, state in (("matched", "accepted"), ("server_pending", "pending_match")):
                result[column] = db.execute("SELECT COUNT(*) " + eligible + " AND o.state='sent' AND d.server_status=?", (state,)).fetchone()[0]
        result["total"] = result["pending"] + result["sent"] + result["rejected"]
        return result
