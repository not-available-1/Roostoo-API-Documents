"""Append-only SQLite event log for a trading decision's full lifecycle."""

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Mapping


_SENSITIVE_KEY = re.compile(r"api.?key|api.?secret|secret|authorization|signature|password|token|credential|private.?key|access.?key", re.I)
_SENSITIVE_TEXT = re.compile(r"bearer\s+\S+|(?:api.?key|api.?secret|authorization|signature|password|token)\s*[:=]\s*\S+", re.I)


def _safe_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): ("[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _safe_json(item))
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    if isinstance(value, str):
        return "[REDACTED]" if _SENSITIVE_TEXT.search(value) else value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite audit decimal")
        return str(value)
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        raise ValueError("binary float is not allowed in audit payload")
    raise TypeError(f"unsupported audit payload type: {type(value).__name__}")


@dataclass(frozen=True)
class AuditEvent:
    """One immutable lifecycle event; fields not relevant to this stage may be null."""

    timestamp: datetime
    event_type: str
    run_id: str
    decision_id: str
    intent_id: str | None = None
    symbol: str | None = None
    side: str | None = None
    price: Decimal | None = None
    quantity: Decimal | None = None
    order_id: str | None = None
    pnl: Decimal | None = None
    signal_reason: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class AuditRecord:
    """Database sequence and decoded event."""

    sequence: int
    event: AuditEvent


class AuditLog:
    """Small transactional API; callers append events and query by decision ID."""

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        with self._conn:
            self._conn.execute("""CREATE TABLE IF NOT EXISTS audit_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                run_id TEXT NOT NULL,
                decision_id TEXT NOT NULL,
                intent_id TEXT,
                symbol TEXT,
                side TEXT,
                price TEXT,
                quantity TEXT,
                order_id TEXT,
                pnl TEXT,
                signal_reason TEXT,
                payload_json TEXT NOT NULL
            )""")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_decision ON audit_events(decision_id, sequence)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_intent ON audit_events(intent_id, sequence)")

    def journal_mode(self) -> str:
        """Expose effective SQLite journal mode for diagnostics."""

        return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0])

    def append(self, event: AuditEvent) -> int:
        """Atomically append one event; redact known credential fields first."""

        if event.timestamp.tzinfo is None or not event.event_type or not event.run_id or not event.decision_id:
            raise ValueError("UTC-aware timestamp, event type, run ID and decision ID required")
        if event.side is not None and event.side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        for name, value in (("price", event.price), ("quantity", event.quantity), ("pnl", event.pnl)):
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
                raise ValueError(f"{name} must be a finite Decimal")
        if event.price is not None and event.price <= 0:
            raise ValueError("price must be positive")
        if event.quantity is not None and event.quantity <= 0:
            raise ValueError("quantity must be positive")
        safe_payload = _safe_json(event.payload)
        payload_json = json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        reason = _safe_json(event.signal_reason) if event.signal_reason is not None else None
        with self._conn:
            cursor = self._conn.execute("""INSERT INTO audit_events
                (timestamp,event_type,run_id,decision_id,intent_id,symbol,side,price,quantity,
                 order_id,pnl,signal_reason,payload_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event.timestamp.isoformat(), event.event_type, event.run_id, event.decision_id,
                 event.intent_id, event.symbol, event.side,
                 str(event.price) if event.price is not None else None,
                 str(event.quantity) if event.quantity is not None else None,
                 event.order_id, str(event.pnl) if event.pnl is not None else None,
                 reason, payload_json))
        return int(cursor.lastrowid)

    def read(self, decision_id: str | None = None) -> tuple[AuditRecord, ...]:
        """Read events in append order, optionally for one trading decision."""

        if decision_id is None:
            rows = self._conn.execute("SELECT * FROM audit_events ORDER BY sequence")
        else:
            rows = self._conn.execute("SELECT * FROM audit_events WHERE decision_id=? ORDER BY sequence",
                                      (decision_id,))
        records = []
        for row in rows:
            event = AuditEvent(datetime.fromisoformat(row["timestamp"]), row["event_type"],
                               row["run_id"], row["decision_id"], row["intent_id"],
                               row["symbol"], row["side"],
                               Decimal(row["price"]) if row["price"] is not None else None,
                               Decimal(row["quantity"]) if row["quantity"] is not None else None,
                               row["order_id"], Decimal(row["pnl"]) if row["pnl"] is not None else None,
                               row["signal_reason"], json.loads(row["payload_json"]))
            records.append(AuditRecord(row["sequence"], event))
        return tuple(records)

    def close(self) -> None:
        """Close the SQLite connection."""

        self._conn.close()

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
