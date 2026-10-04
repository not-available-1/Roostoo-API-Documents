import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

from d_layer.audit import AuditEvent, AuditLog


NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


class AuditTests(unittest.TestCase):
    def test_create_append_read_reopen_and_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.sqlite3"
            event = AuditEvent(NOW, "fill", "run-1", "decision-1", intent_id="intent-1",
                               symbol="BTC", side="BUY", price=D("100.25"), quantity=D("2"),
                               order_id="order-1", pnl=D("1.50"), signal_reason="fixture",
                               payload={"response": {"status": "filled"}})
            with AuditLog(path) as log:
                sequence = log.append(event)
                self.assertEqual(sequence, 1)
                self.assertEqual(log.journal_mode(), "wal")
            with AuditLog(path) as log:
                records = log.read(decision_id="decision-1")
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].event.timestamp, NOW)
                self.assertEqual(records[0].event.price, D("100.25"))
                self.assertEqual(records[0].event.order_id, "order-1")
                self.assertEqual(records[0].event.payload["response"]["status"], "filled")
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM audit_events").fetchone()[0], 1)

    def test_sensitive_nested_response_fields_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.sqlite3"
            event = AuditEvent(NOW, "broker_response", "run-1", "decision-1",
                               payload={"response": {"API_KEY": "do-not-store",
                                                     "headers": {"Authorization": "Bearer do-not-store"},
                                                     "status": "ok"}})
            with AuditLog(path) as log:
                log.append(event)
                stored = log.read()[0].event.payload
            self.assertEqual(stored["response"]["API_KEY"], "[REDACTED]")
            self.assertEqual(stored["response"]["headers"]["Authorization"], "[REDACTED]")
            self.assertNotIn("do-not-store", path.read_bytes().decode("utf-8", errors="ignore"))

    def test_invalid_event_or_nonfinite_payload_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with AuditLog(Path(directory) / "audit.sqlite3") as log:
                with self.assertRaises(ValueError):
                    log.append(AuditEvent(NOW, "", "run-1", "decision-1"))
                with self.assertRaises(ValueError):
                    log.append(AuditEvent(NOW, "risk", "run-1", "decision-1",
                                          payload={"number": D("NaN")}))

    def test_committed_wal_event_survives_exit_without_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.sqlite3"
            child = (
                "import os, sys; "
                "from datetime import datetime, timezone; "
                "from d_layer.audit import AuditEvent, AuditLog; "
                "log = AuditLog(sys.argv[1]); "
                "log.append(AuditEvent(datetime.now(timezone.utc), 'risk', 'run', 'decision')); "
                "os._exit(0)"
            )
            subprocess.run([sys.executable, "-c", child, str(path)], check=True)
            with AuditLog(path) as log:
                self.assertEqual(len(log.read(decision_id="decision")), 1)


if __name__ == "__main__":
    unittest.main()
