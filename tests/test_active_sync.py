import tempfile
import unittest
from pathlib import Path

from src.db import PhipDB


class ActiveSyncTests(unittest.TestCase):
    def test_sync_active_sources_marks_missing_items_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = PhipDB(Path(tmp) / "phip.db")
            db.upsert_discovered(
                source_url="https://example.com/current.pdf",
                company_name="Current Co",
                stock_code=None,
                board="main",
                document_type="PHIP",
                publish_date="2026-05-11",
                sponsor=None,
            )
            db.upsert_discovered(
                source_url="https://example.com/delisted.pdf",
                company_name="Delisted Co",
                stock_code=None,
                board="main",
                document_type="PHIP",
                publish_date="2026-04-26",
                sponsor=None,
            )
            db.update("https://example.com/delisted.pdf",
                      status="REPORTED", report_path="report.docx")

            changed = db.sync_active_sources(
                {"https://example.com/current.pdf"},
                boards=["main"],
                doc_types=["PHIP"],
            )

            self.assertEqual(changed, 1)
            current = db.get("https://example.com/current.pdf")
            delisted = db.get("https://example.com/delisted.pdf")
            self.assertEqual(current["active"], 1)
            self.assertEqual(delisted["active"], 0)
            self.assertEqual(delisted["status"], "REPORTED")
            self.assertEqual(delisted["report_path"], "report.docx")

    def test_list_pending_excludes_inactive_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = PhipDB(Path(tmp) / "phip.db")
            db.upsert_discovered(
                source_url="https://example.com/inactive.pdf",
                company_name="Inactive Co",
                stock_code=None,
                board="main",
                document_type="PHIP",
                publish_date="2026-05-01",
                sponsor=None,
            )
            db.sync_active_sources(set(), boards=["main"], doc_types=["PHIP"])

            self.assertEqual(db.list_pending(), [])

    def test_upsert_discovered_reactivates_existing_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = PhipDB(Path(tmp) / "phip.db")
            db.upsert_discovered(
                source_url="https://example.com/returning.pdf",
                company_name="Old Name",
                stock_code=None,
                board="main",
                document_type="PHIP",
                publish_date="2026-05-01",
                sponsor=None,
            )
            db.sync_active_sources(set(), boards=["main"], doc_types=["PHIP"])

            is_new = db.upsert_discovered(
                source_url="https://example.com/returning.pdf",
                company_name="New Name",
                stock_code=None,
                board="main",
                document_type="PHIP",
                publish_date="2026-05-02",
                sponsor=None,
            )

            row = db.get("https://example.com/returning.pdf")
            self.assertFalse(is_new)
            self.assertEqual(row["active"], 1)
            self.assertEqual(row["company_name"], "New Name")
            self.assertEqual(row["publish_date"], "2026-05-02")


if __name__ == "__main__":
    unittest.main()
