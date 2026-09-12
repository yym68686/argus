import importlib.util
import pathlib
import sqlite3
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "codex_log_maintenance", ROOT / "scripts/codex_log_maintenance.py"
)
maintenance = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = maintenance
SPEC.loader.exec_module(maintenance)


class CodexLogMaintenanceTests(unittest.TestCase):
    def _create_log_database(self, path: pathlib.Path) -> None:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("VACUUM")
        conn.execute(
            "CREATE TABLE logs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "feedback_log_body TEXT, estimated_bytes INTEGER)"
        )
        payload = "x" * 4096
        conn.executemany(
            "INSERT INTO logs (feedback_log_body, estimated_bytes) VALUES (?, ?)",
            [(payload, len(payload)) for _ in range(80)],
        )
        conn.commit()
        conn.close()

    def test_keeps_recent_logs_and_reclaims_incremental_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = pathlib.Path(tempdir) / "logs_2.sqlite"
            self._create_log_database(path)
            before_size = path.stat().st_size

            result = maintenance.maintain_log_database(
                path,
                maintenance.Settings(
                    max_rows=10,
                    max_estimated_bytes=0,
                    max_vacuum_pages=100_000,
                ),
            )

            self.assertEqual(result.status, "maintained")
            self.assertEqual(result.deleted_rows, 70)
            self.assertGreater(result.reclaimed_pages, 0)
            self.assertLess(path.stat().st_size, before_size)
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0], 10)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            conn.close()

    def test_rejects_non_log_database(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = pathlib.Path(tempdir) / "logs_2.sqlite"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE state (id INTEGER PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO state VALUES (1, 'preserve')")
            conn.commit()
            conn.close()

            result = maintenance.maintain_log_database(path, maintenance.Settings())

            self.assertEqual(result.status, "schema-mismatch")
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("SELECT value FROM state").fetchone()[0], "preserve")
            conn.close()

    def test_rejects_log_table_without_incremental_auto_vacuum(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = pathlib.Path(tempdir) / "logs_2.sqlite"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE logs (id INTEGER PRIMARY KEY, feedback_log_body TEXT, estimated_bytes INTEGER)"
            )
            conn.commit()
            conn.close()

            result = maintenance.maintain_log_database(path, maintenance.Settings())

            self.assertEqual(result.status, "schema-mismatch")

    def test_estimated_size_limit_keeps_the_newest_row(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = pathlib.Path(tempdir) / "logs_2.sqlite"
            self._create_log_database(path)

            result = maintenance.maintain_log_database(
                path,
                maintenance.Settings(
                    max_rows=0,
                    max_estimated_bytes=1,
                    max_vacuum_pages=0,
                ),
            )

            self.assertEqual(result.status, "maintained")
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0], 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
