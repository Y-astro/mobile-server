"""
tests/test_repertoire.py: Isolated unit and contract tests for repertoire.py dispatch.
"""
import gzip
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import repertoire


def create_fake_workbook(composition_name="Vatapi Ganapatim", records_count=1):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Data" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>""",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""",
        )
        fields = [
            "No.", "Composition", "rAga", "tALa", "Composer", "Comp_type",
            "Language", "Subject", "Repertoire", "pATAntara", "ConcertFit",
            "Teaching", "Rep date", "Remarks", "Reference"
        ]
        cols = [chr(ord("A") + i) for i in range(len(fields))]
        header_cells = "".join(
            f'<c r="{col}1" t="inlineStr"><is><t>{f}</t></is></c>'
            for col, f in zip(cols, fields)
        )

        rows = [f'<row r="1">{header_cells}</row>']
        for idx in range(1, records_count + 1):
            row_idx = idx + 1
            vals = [
                str(idx), f"{composition_name} {idx}", "Hamsadhvani", "Adi",
                "Muthuswami Dikshitar", "Kriti", "Sanskrit", "Ganesha",
                "Core", "Standard", "Opener", "Advanced", "2026-01-01",
                "Concert piece", "https://example.com"
            ]
            row_cells = "".join(
                f'<c r="{col}{row_idx}" t="inlineStr"><is><t>{v}</t></is></c>'
                for col, v in zip(cols, vals)
            )
            rows.append(f'<row r="{row_idx}">{row_cells}</row>')

        sheet_content = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(rows)}</sheetData></worksheet>'
        )
        zf.writestr("xl/worksheets/sheet1.xml", sheet_content)
    return buf.getvalue()


class MockHandler:
    def __init__(self, method="GET", path="/", headers=None, body=b""):
        self.command = method
        self.path = path
        self.headers = headers or {}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = {}
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


class TestRepertoireDispatch(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        # Isolated static directory
        self.static_dir = self.root / "static"
        self.static_dir.mkdir(parents=True, exist_ok=True)
        (self.static_dir / "repertoire.html").write_text("<!DOCTYPE html><html><body>Repertoire</body></html>", encoding="utf-8")
        (self.static_dir / "repertoire.js").write_text("console.log('repertoire');", encoding="utf-8")
        (self.static_dir / "repertoire.css").write_text("body { color: black; }", encoding="utf-8")

        # Isolated version store
        self.store_folder = self.root / "store"
        self.settings_dir = self.root / "settings"
        self.legacy_file = self.root / "legacy.xlsx"
        self.store_folder.mkdir(parents=True, exist_ok=True)
        self.settings_dir.mkdir(parents=True, exist_ok=True)

        self.store = repertoire.VersionStore(self.store_folder, self.settings_dir, self.legacy_file)
        initial_wb = create_fake_workbook("Vatapi", 1)
        self.store.upload(initial_wb)

        # Patch repertoire module globals
        self.static_patch = patch.object(repertoire, "STATIC", self.static_dir)
        self.store_patch = patch.object(repertoire, "STORE", self.store)
        self.static_patch.start()
        self.store_patch.start()
        repertoire._cache = None

    def tearDown(self):
        self.store_patch.stop()
        self.static_patch.stop()
        repertoire._cache = None
        self.temp_dir.cleanup()

    def test_unhandled_routes_return_false(self):
        h_get = MockHandler(method="GET", path="/api/other")
        self.assertFalse(repertoire.dispatch_repertoire(h_get, user="admin"))

        h_post = MockHandler(method="POST", path="/api/other")
        self.assertFalse(repertoire.dispatch_repertoire_post(h_post, user="admin"))

    def test_anonymous_requests_denied(self):
        # UI routes redirect to /login with 303
        for path in ("/repertoire", "/repertoire/"):
            with self.subTest(path=path):
                h = MockHandler(method="GET", path=path)
                handled = repertoire.dispatch_repertoire(h, user=None)
                self.assertTrue(handled)
                self.assertEqual(h.status, 303)
                self.assertEqual(h.response_headers.get("Location"), "/login")

        # Static assets and APIs return 401 JSON for anonymous requests
        for path in ("/repertoire/app.js", "/repertoire/app.css", "/api/repertoire", "/api/repertoire/versions"):
            with self.subTest(path=path):
                h = MockHandler(method="GET", path=path)
                handled = repertoire.dispatch_repertoire(h, user=None)
                self.assertTrue(handled)
                self.assertEqual(h.status, 401)
                self.assertIn("Please sign in", h.wfile.getvalue().decode("utf-8"))

        # POST APIs return 401 JSON for anonymous requests
        for path in ("/api/repertoire/upload", "/api/repertoire/select"):
            with self.subTest(path=path):
                h = MockHandler(method="POST", path=path, headers={"X-Repertoire-Request": "1"})
                handled = repertoire.dispatch_repertoire_post(h, user=None)
                self.assertTrue(handled)
                self.assertEqual(h.status, 401)
                self.assertIn("Please sign in", h.wfile.getvalue().decode("utf-8"))

    def test_authenticated_static_and_catalogue_routes(self):
        for role in ("admin", "family"):
            with self.subTest(role=role):
                # UI HTML
                h_ui = MockHandler(method="GET", path="/repertoire/")
                self.assertTrue(repertoire.dispatch_repertoire(h_ui, user=role))
                self.assertEqual(h_ui.status, 200)
                self.assertIn(b"Repertoire", h_ui.wfile.getvalue())

                # JS asset
                h_js = MockHandler(method="GET", path="/repertoire/app.js")
                self.assertTrue(repertoire.dispatch_repertoire(h_js, user=role))
                self.assertEqual(h_js.status, 200)
                self.assertEqual(h_js.response_headers.get("Content-Type"), "text/javascript; charset=utf-8")

                # CSS asset
                h_css = MockHandler(method="GET", path="/repertoire/app.css")
                self.assertTrue(repertoire.dispatch_repertoire(h_css, user=role))
                self.assertEqual(h_css.status, 200)
                self.assertEqual(h_css.response_headers.get("Content-Type"), "text/css; charset=utf-8")

                # Versions metadata API
                h_ver = MockHandler(method="GET", path="/api/repertoire/versions")
                self.assertTrue(repertoire.dispatch_repertoire(h_ver, user=role))
                self.assertEqual(h_ver.status, 200)
                v_data = json.loads(h_ver.wfile.getvalue().decode("utf-8"))
                self.assertEqual(v_data.get("active"), "current")

                # Catalogue API
                h_cat = MockHandler(method="GET", path="/api/repertoire")
                self.assertTrue(repertoire.dispatch_repertoire(h_cat, user=role))
                self.assertEqual(h_cat.status, 200)
                c_data = json.loads(h_cat.wfile.getvalue().decode("utf-8"))
                self.assertEqual(len(c_data["records"]), 1)
                self.assertIn("Vatapi", c_data["records"][0]["Composition"])

                # Gzip compression support
                h_gzip = MockHandler(method="GET", path="/api/repertoire", headers={"Accept-Encoding": "gzip, deflate"})
                self.assertTrue(repertoire.dispatch_repertoire(h_gzip, user=role))
                self.assertEqual(h_gzip.status, 200)
                self.assertEqual(h_gzip.response_headers.get("Content-Encoding"), "gzip")
                decompressed = gzip.decompress(h_gzip.wfile.getvalue()).decode("utf-8")
                self.assertIn("Vatapi", decompressed)

    def test_post_origin_and_header_guards(self):
        # Missing X-Repertoire-Request header returns 403
        h_no_hdr = MockHandler(method="POST", path="/api/repertoire/select")
        self.assertTrue(repertoire.dispatch_repertoire_post(h_no_hdr, user="family"))
        self.assertEqual(h_no_hdr.status, 403)

        # Cross-site Sec-Fetch-Site returns 403
        h_cross = MockHandler(
            method="POST",
            path="/api/repertoire/select",
            headers={"X-Repertoire-Request": "1", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertTrue(repertoire.dispatch_repertoire_post(h_cross, user="family"))
        self.assertEqual(h_cross.status, 403)

    def test_upload_and_select_role_contracts(self):
        """
        Preserve EXISTING upload authorization: verify both admin and family
        are permitted to upload and select workbooks under current implementation.
        """
        # Family upload valid second workbook
        new_wb = create_fake_workbook("Endaro Mahanubhavulu", 2)
        h_upload = MockHandler(
            method="POST",
            path="/api/repertoire/upload",
            headers={"X-Repertoire-Request": "1", "Content-Length": str(len(new_wb))},
            body=new_wb,
        )
        self.assertTrue(repertoire.dispatch_repertoire_post(h_upload, user="family"))
        self.assertEqual(h_upload.status, 200)
        up_resp = json.loads(h_upload.wfile.getvalue().decode("utf-8"))
        self.assertEqual(up_resp["versions"]["active"], "current")
        self.assertIsNotNone(up_resp["versions"]["previous"])

        # Admin selects 'previous' workbook
        select_prev_body = json.dumps({"version": "previous"}).encode("utf-8")
        h_select_admin = MockHandler(
            method="POST",
            path="/api/repertoire/select",
            headers={"X-Repertoire-Request": "1", "Content-Length": str(len(select_prev_body))},
            body=select_prev_body,
        )
        self.assertTrue(repertoire.dispatch_repertoire_post(h_select_admin, user="admin"))
        self.assertEqual(h_select_admin.status, 200)
        sel_resp = json.loads(h_select_admin.wfile.getvalue().decode("utf-8"))
        self.assertEqual(sel_resp["versions"]["active"], "previous")

        # Family selects 'current' workbook back
        select_curr_body = json.dumps({"version": "current"}).encode("utf-8")
        h_select_family = MockHandler(
            method="POST",
            path="/api/repertoire/select",
            headers={"X-Repertoire-Request": "1", "Content-Length": str(len(select_curr_body))},
            body=select_curr_body,
        )
        self.assertTrue(repertoire.dispatch_repertoire_post(h_select_family, user="family"))
        self.assertEqual(h_select_family.status, 200)

    def test_upload_size_and_format_validation(self):
        # Oversized upload (> 8MB) returns 413
        h_too_large = MockHandler(
            method="POST",
            path="/api/repertoire/upload",
            headers={"X-Repertoire-Request": "1", "Content-Length": str(8 * 1024 * 1024 + 1)},
        )
        self.assertTrue(repertoire.dispatch_repertoire_post(h_too_large, user="admin"))
        self.assertEqual(h_too_large.status, 413)

        # Invalid workbook content returns 400
        invalid_body = b"not-a-valid-zip-or-excel"
        h_invalid = MockHandler(
            method="POST",
            path="/api/repertoire/upload",
            headers={"X-Repertoire-Request": "1", "Content-Length": str(len(invalid_body))},
            body=invalid_body,
        )
        self.assertTrue(repertoire.dispatch_repertoire_post(h_invalid, user="admin"))
        self.assertEqual(h_invalid.status, 400)

        # Invalid select version returns 400
        bad_select = json.dumps({"version": "invalid_version"}).encode("utf-8")
        h_bad_select = MockHandler(
            method="POST",
            path="/api/repertoire/select",
            headers={"X-Repertoire-Request": "1", "Content-Length": str(len(bad_select))},
            body=bad_select,
        )
        self.assertTrue(repertoire.dispatch_repertoire_post(h_bad_select, user="admin"))
        self.assertEqual(h_bad_select.status, 400)


if __name__ == "__main__":
    unittest.main()
