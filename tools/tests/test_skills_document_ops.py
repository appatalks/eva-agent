"""Offline fixture contracts for bounded document and MCP-builder skills."""

import importlib.util
import http.client
import json
import os
import shutil
import sys
import subprocess
import socket
import tempfile
import threading
import urllib.request
import unittest
import zipfile
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from skills import execute_bounded_skill
from skills import document_ops
from bridge import core as bridge_core


HAS_DOCX = importlib.util.find_spec("docx") is not None
HAS_PPTX = importlib.util.find_spec("pptx") is not None
HAS_REPORTLAB = importlib.util.find_spec("reportlab") is not None
HAS_PDFPLUMBER = importlib.util.find_spec("pdfplumber") is not None
HAS_OCR = all((importlib.util.find_spec(name) is not None for name in ("pypdf", "pytesseract", "PIL"))) and all(shutil.which(name) for name in ("pdftoppm", "tesseract"))
HAS_LIBREOFFICE = bool(shutil.which("libreoffice") or shutil.which("soffice"))


class BoundedSkillTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="eva-bounded-skills-")
        self.artifacts = self.directory.name

    def tearDown(self):
        self.directory.cleanup()

    def run_skill(self, skill, operation, **extra):
        request = {"skill": skill, "operation": operation}
        request.update(extra)
        return execute_bounded_skill(request, artifacts_dir=self.artifacts)

    def _write_fillable_pdf(self, name="fillable.pdf"):
        from reportlab.pdfgen import canvas

        path = os.path.join(self.artifacts, name)
        document = canvas.Canvas(path)
        document.drawString(72, 760, "Customer name")
        document.acroForm.textfield(name="customer_name", x=72, y=710, width=240, height=24, value="")
        document.acroForm.checkbox(name="confirmed", x=72, y=650, buttonStyle="check")
        document.save()
        return path

    def _write_table_pdf(self, name="table.pdf"):
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

        path = os.path.join(self.artifacts, name)
        table = Table([["Name", "Total"], ["Ada", "3"], ["Grace", "7"]])
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ]))
        document = SimpleDocTemplate(path, pagesize=letter)
        document.build([table])
        return path

    def _write_ocr_pdf(self, name="ocr.pdf"):
        from PIL import Image, ImageDraw
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas

        image_path = os.path.join(self.artifacts, "ocr-source.png")
        image = Image.new("RGB", (1200, 240), "white")
        ImageDraw.Draw(image).text((60, 80), "EVA OCR FIXTURE 2468", fill="black")
        image.save(image_path)
        pdf_path = os.path.join(self.artifacts, name)
        document = canvas.Canvas(pdf_path, pagesize=(600, 120))
        document.drawImage(ImageReader(image_path), 0, 0, width=600, height=120)
        document.save()
        return pdf_path

    def test_pdf_create_read_validate_merge_split_extract(self):
        first = self.run_skill("pdf", "create", output="first.pdf", options={"text": "first page"})
        second = self.run_skill("pdf", "create", output="second.pdf", options={"text": "second page"})
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        read = self.run_skill("pdf", "read", input="first.pdf")
        self.assertTrue(read["ok"], read)
        self.assertIn("first page", read["result"]["text"])
        merged = self.run_skill("pdf", "merge", inputs=["first.pdf", "second.pdf"], output="merged.pdf")
        self.assertTrue(merged["ok"], merged)
        split = self.run_skill("pdf", "split", input="merged.pdf", output="split.pdf", options={"page": 2})
        self.assertTrue(split["ok"], split)
        extracted = self.run_skill("pdf", "extract", input="merged.pdf", output="merged.txt")
        self.assertTrue(extracted["ok"], extracted)
        with open(os.path.join(self.artifacts, "merged.txt"), encoding="utf-8") as stream:
            self.assertIn("second page", stream.read())

    def test_pdf_creation_wraps_long_lines_without_discarding_content(self):
        long_line = "START " + ("content " * 250) + "END"
        created = self.run_skill("pdf", "create", output="wrapped.pdf", options={"text": long_line})
        self.assertTrue(created["ok"], created)
        read = self.run_skill("pdf", "read", input="wrapped.pdf")
        self.assertTrue(read["ok"], read)
        self.assertIn("START", read["result"]["text"])
        self.assertIn("END", read["result"]["text"])

    def test_xlsx_create_read_edit_validate_formula(self):
        created = self.run_skill("xlsx", "create", output="book.xlsx", options={"rows": [[1, 2], ["=SUM(A1:B1)", 4]]})
        self.assertTrue(created["ok"], created)
        read = self.run_skill("xlsx", "read", input="book.xlsx")
        self.assertTrue(read["ok"], read)
        self.assertIn("Sheet!A2", read["result"]["formulas"])
        edited = self.run_skill("xlsx", "edit", input="book.xlsx", output="edited.xlsx", options={"cells": {"B2": 9}})
        self.assertTrue(edited["ok"], edited)

    def test_xlsx_request_and_result_limits(self):
        oversized_rows = self.run_skill("xlsx", "create", output="too-many-rows.xlsx", options={"rows": [[1]] * 1001})
        self.assertFalse(oversized_rows["ok"])
        self.assertEqual(oversized_rows["error"]["code"], "input_too_large")
        created = self.run_skill("xlsx", "create", output="wide.xlsx", options={"rows": [["x" * 200] * 100] * 100})
        self.assertTrue(created["ok"], created)
        read = self.run_skill("xlsx", "read", input="wide.xlsx")
        self.assertFalse(read["ok"])
        self.assertEqual(read["error"]["code"], "result_too_large")

    def test_xlsx_compression_ratio_is_rejected_before_parsing(self):
        created = self.run_skill("xlsx", "create", output="compressed.xlsx", options={"rows": [["safe"]]})
        self.assertTrue(created["ok"], created)
        with zipfile.ZipFile(os.path.join(self.artifacts, "compressed.xlsx"), "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/media/repetitive.bin", b"x" * (2 * 1024 * 1024))
        receipt = self.run_skill("xlsx", "validate", input="compressed.xlsx")
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["error"]["code"], "input_too_large")
        with patch("skills.document_ops.importlib.import_module") as module_loader:
            receipt = self.run_skill("xlsx", "read", input="compressed.xlsx")
        self.assertFalse(receipt["ok"])
        module_loader.assert_not_called()

    def test_docx_and_pptx_archive_preflight_rejects_compression_bombs(self):
        for extension in ("docx", "pptx"):
            path = os.path.join(self.artifacts, "compressed." + extension)
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("word/repetitive.bin", b"x" * (2 * 1024 * 1024))
            with self.assertRaises(document_ops.SkillExecutionError) as error:
                document_ops._preflight_ooxml_archive(path)
            self.assertEqual(error.exception.code, "input_too_large")

    def test_second_approved_workspace_root_is_addressable(self):
        first_root = os.path.join(self.artifacts, "workspace-one")
        second_root = os.path.join(self.artifacts, "workspace-two")
        os.makedirs(first_root)
        os.makedirs(second_root)
        receipt = execute_bounded_skill(
            {"skill": "mcp-builder", "operation": "scaffold", "root": "workspace-2", "output": "demo", "options": {"language": "python"}},
            artifacts_dir=self.artifacts,
            approved_workspace_roots=[first_root, second_root],
        )
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["output"]["root"], "workspace-2")

    @unittest.skipUnless(HAS_LIBREOFFICE, "LibreOffice is not installed")
    def test_xlsx_recalculate_writes_distinct_output_and_exposes_cached_result(self):
        created = self.run_skill("xlsx", "create", output="formula-input.xlsx", options={"rows": [[1, 2], ["=SUM(A1:B1)", 4]]})
        self.assertTrue(created["ok"], created)
        recalculated = self.run_skill("xlsx", "recalculate", input="formula-input.xlsx", output="formula-output.xlsx")
        self.assertTrue(recalculated["ok"], recalculated)
        self.assertEqual(recalculated["output"]["path"], "formula-output.xlsx")
        self.assertEqual(recalculated["validation"]["formula_errors"], [])
        from openpyxl import load_workbook

        cached = load_workbook(os.path.join(self.artifacts, "formula-output.xlsx"), data_only=True)
        self.assertEqual(cached["Sheet"]["A2"].value, 3)

    @unittest.skipUnless(HAS_REPORTLAB and HAS_PDFPLUMBER, "reportlab and pdfplumber are not installed")
    def test_pdf_form_inspect_fill_and_tables(self):
        self._write_fillable_pdf()
        inspected = self.run_skill("pdf", "inspect-form", input="fillable.pdf")
        self.assertTrue(inspected["ok"], inspected)
        fields = {field["name"]: field for field in inspected["result"]["fields"]}
        self.assertEqual(fields["customer_name"]["type"], "/Tx")
        self.assertIn("confirmed", fields)
        filled = self.run_skill("pdf", "fill-form", input="fillable.pdf", output="filled.pdf", options={"fields": {"customer_name": "Ada Lovelace"}})
        self.assertTrue(filled["ok"], filled)
        self.assertEqual(next(field["value"] for field in filled["result"]["fields"] if field["name"] == "customer_name"), "Ada Lovelace")
        unknown = self.run_skill("pdf", "fill-form", input="fillable.pdf", output="unknown.pdf", options={"fields": {"missing": "blocked"}})
        self.assertFalse(unknown["ok"])
        self.assertEqual(unknown["error"]["code"], "unknown_field")
        self._write_table_pdf()
        tables = self.run_skill("pdf", "tables", input="table.pdf")
        self.assertTrue(tables["ok"], tables)
        self.assertEqual(tables["result"]["pages"][0]["tables"][0][1][0], "Ada")
        self.assertIn("Name", tables["result"]["pages"][0]["text"] or "Name")

    @unittest.skipUnless(HAS_OCR, "Poppler, Tesseract, pytesseract, Pillow, and pypdf are required")
    def test_pdf_ocr_renders_real_image_and_writes_text(self):
        self._write_ocr_pdf()
        ocr = self.run_skill("pdf", "ocr", input="ocr.pdf", output="ocr.txt")
        self.assertTrue(ocr["ok"], ocr)
        with open(os.path.join(self.artifacts, "ocr.txt"), encoding="utf-8") as stream:
            text = stream.read().upper()
        self.assertIn("EVA", text)
        self.assertIn("FIXTURE", text)

    @unittest.skipUnless(HAS_DOCX and HAS_LIBREOFFICE, "python-docx and LibreOffice are not installed")
    def test_docx_render_to_pdf(self):
        created = self.run_skill("docx", "create", output="render.docx", options={"text": "DOCX render fixture"})
        self.assertTrue(created["ok"], created)
        rendered = self.run_skill("docx", "render", input="render.docx", output="render.pdf")
        self.assertTrue(rendered["ok"], rendered)
        self.assertEqual(rendered["validation"]["status"], "valid")

    @unittest.skipUnless(HAS_PPTX and HAS_LIBREOFFICE, "python-pptx and LibreOffice are not installed")
    def test_pptx_render_to_pdf(self):
        created = self.run_skill("pptx", "create", output="render.pptx", options={"title": "Render", "body": "PPTX render fixture"})
        self.assertTrue(created["ok"], created)
        rendered = self.run_skill("pptx", "render", input="render.pptx", output="render.pdf")
        self.assertTrue(rendered["ok"], rendered)
        self.assertEqual(rendered["validation"]["status"], "valid")

    @unittest.skipUnless(HAS_DOCX, "python-docx is not installed")
    def test_docx_create_read_edit_validate_metadata(self):
        created = self.run_skill("docx", "create", output="note.docx", options={"text": "alpha", "metadata": {"title": "Eva test", "author": "Eva"}})
        self.assertTrue(created["ok"], created)
        edited = self.run_skill("docx", "edit", input="note.docx", output="edited.docx", options={"replace": {"find": "alpha", "replace": "beta"}, "append_text": "tail"})
        self.assertTrue(edited["ok"], edited)
        read = self.run_skill("docx", "read", input="edited.docx")
        self.assertTrue(read["ok"], read)
        self.assertIn("beta", read["result"]["text"])
        self.assertIn("tail", read["result"]["text"])
        self.assertEqual(read["result"]["metadata"]["title"], "Eva test")

    @unittest.skipUnless(HAS_PPTX, "python-pptx is not installed")
    def test_pptx_create_read_edit_validate(self):
        created = self.run_skill("pptx", "create", output="deck.pptx", options={"title": "Alpha", "body": "Beta"})
        self.assertTrue(created["ok"], created)
        edited = self.run_skill("pptx", "edit", input="deck.pptx", output="edited.pptx", options={"replace": {"find": "Beta", "replace": "Gamma"}})
        self.assertTrue(edited["ok"], edited)
        read = self.run_skill("pptx", "read", input="edited.pptx")
        self.assertTrue(read["ok"], read)
        self.assertIn("Gamma", read["result"]["text"])

    def test_malformed_traversal_symlink_and_receipt_contracts(self):
        with open(os.path.join(self.artifacts, "bad.pdf"), "wb") as stream:
            stream.write(b"not a pdf")
        malformed = self.run_skill("pdf", "validate", input="bad.pdf")
        self.assertFalse(malformed["ok"])
        self.assertIn("error", malformed)
        traversal = self.run_skill("pdf", "create", output="../escape.pdf", options={"text": "blocked"})
        self.assertFalse(traversal["ok"])
        self.assertEqual(traversal["error"]["code"], "invalid_path")
        outside = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        outside.close()
        try:
            os.symlink(outside.name, os.path.join(self.artifacts, "link.pdf"))
            symlink = self.run_skill("pdf", "validate", input="link.pdf")
            self.assertFalse(symlink["ok"])
            self.assertEqual(symlink["error"]["code"], "symlink_rejected")
        finally:
            os.unlink(outside.name)
        unsupported = self.run_skill("pdf", "create", output="bad.docx", options={"text": "blocked"})
        self.assertFalse(unsupported["ok"])
        self.assertEqual(unsupported["error"]["code"], "unsupported_extension")
        self.assertEqual(set(("operation", "input", "output", "validation", "warnings", "error")) - set(unsupported), set())

    def test_missing_dependency_is_actionable_and_does_not_install(self):
        with patch("skills.document_ops.importlib.import_module", side_effect=ImportError("test missing")):
            receipt = self.run_skill("docx", "create", output="missing.docx", options={"text": "x"})
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["error"]["code"], "missing_dependency")
        self.assertIn("python-docx", receipt["error"]["details"]["install"])
        self.assertFalse(os.path.exists(os.path.join(self.artifacts, "missing.docx")))

    def test_office_timeout_is_structured_and_does_not_mutate_input(self):
        created = self.run_skill("xlsx", "create", output="timeout-input.xlsx", options={"rows": [[1, 2], ["=SUM(A1:B1)"]]})
        self.assertTrue(created["ok"], created)
        with patch("skills.document_ops.shutil.which", return_value="/usr/bin/libreoffice"), patch(
            "skills.document_ops.subprocess.run", side_effect=subprocess.TimeoutExpired("libreoffice", 90)
        ):
            receipt = self.run_skill("xlsx", "recalculate", input="timeout-input.xlsx", output="timeout-output.xlsx")
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["error"]["code"], "timeout")
        self.assertFalse(os.path.exists(os.path.join(self.artifacts, "timeout-output.xlsx")))

    def test_missing_libreoffice_is_actionable_and_does_not_create_output(self):
        created = self.run_skill("xlsx", "create", output="missing-office-input.xlsx", options={"rows": [[1, 2], ["=SUM(A1:B1)"]]})
        self.assertTrue(created["ok"], created)
        with patch("skills.document_ops.shutil.which", return_value=None):
            receipt = self.run_skill("xlsx", "recalculate", input="missing-office-input.xlsx", output="missing-office-output.xlsx")
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["error"]["code"], "missing_dependency")
        self.assertIn("libreoffice", receipt["error"]["details"]["package"])
        self.assertFalse(os.path.exists(os.path.join(self.artifacts, "missing-office-output.xlsx")))

    def test_bridge_http_execute_success_and_traversal_rejection(self):
        server = bridge_core.ThreadingHTTPServer(("127.0.0.1", 0), bridge_core.BridgeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:%d/v1/skills/execute" % server.server_address[1]

        def request(payload):
            body = json.dumps(payload).encode("utf-8")
            request_object = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request_object) as response:
                return json.load(response)

        try:
            with patch.object(bridge_core, "_is_loopback_bind", return_value=True), patch.dict(os.environ, {"EVA_BRIDGE_TOKEN": ""}, clear=False), patch.object(bridge_core._cfg, "ARTIFACTS_DIR", self.artifacts):
                success = request({"skill": "pdf", "operation": "create", "output": "http.pdf", "options": {"text": "HTTP fixture"}})
                traversal = request({"skill": "pdf", "operation": "create", "output": "../escape.pdf", "options": {"text": "blocked"}})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertTrue(success["ok"], success)
        self.assertEqual(success["output"]["path"], "http.pdf")
        self.assertFalse(traversal["ok"])
        self.assertEqual(traversal["error"]["code"], "invalid_path")

    def test_bridge_http_execute_rejects_oversized_body(self):
        server = bridge_core.ThreadingHTTPServer(("127.0.0.1", 0), bridge_core.BridgeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:%d/v1/skills/execute" % server.server_address[1]
        body = json.dumps({"skill": "pdf", "operation": "create", "output": "large.pdf", "options": {"text": "x" * (1024 * 1024)}}).encode("utf-8")
        try:
            with patch.object(bridge_core, "_is_loopback_bind", return_value=True), patch.dict(os.environ, {"EVA_BRIDGE_TOKEN": ""}, clear=False), self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(error.exception.code, 413)

    def test_bridge_http_execute_rejects_negative_content_length(self):
        server = bridge_core.ThreadingHTTPServer(("127.0.0.1", 0), bridge_core.BridgeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
        try:
            with patch.object(bridge_core, "_is_loopback_bind", return_value=True), patch.dict(os.environ, {"EVA_BRIDGE_TOKEN": ""}, clear=False):
                connection.putrequest("POST", "/v1/skills/execute")
                connection.putheader("Content-Length", "-1")
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_bridge_http_execute_rejects_duplicate_content_length_and_closes_connection(self):
        server = bridge_core.ThreadingHTTPServer(("127.0.0.1", 0), bridge_core.BridgeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(bridge_core, "_is_loopback_bind", return_value=True), patch.dict(os.environ, {"EVA_BRIDGE_TOKEN": ""}, clear=False), socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2) as connection:
                connection.sendall(
                    b"POST /v1/skills/execute HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}"
                )
                response = connection.recv(4096)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertIn(b"400", response)
        self.assertIn(b"Connection: close", response)

    def test_bridge_http_execute_rejects_transfer_encoding_and_closes_connection(self):
        server = bridge_core.ThreadingHTTPServer(("127.0.0.1", 0), bridge_core.BridgeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(bridge_core, "_is_loopback_bind", return_value=True), patch.dict(os.environ, {"EVA_BRIDGE_TOKEN": ""}, clear=False), socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2) as connection:
                connection.sendall(
                    b"POST /v1/skills/execute HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}"
                )
                response = connection.recv(4096)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertIn(b"400", response)
        self.assertIn(b"Connection: close", response)

    def test_unknown_operations_are_rejected(self):
        receipt = self.run_skill("docx", "delete", output="should-not-exist.docx")
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["error"]["code"], "unsupported_operation")
        receipt = self.run_skill("mcp-builder", "read", input="missing-project")
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["error"]["code"], "unsupported_operation")

    def test_mcp_scaffold_and_validation_without_network(self):
        scaffold = self.run_skill("mcp-builder", "scaffold", output="demo-server", options={"language": "python"})
        self.assertTrue(scaffold["ok"], scaffold)
        self.assertEqual(scaffold["result"]["files"], ["server.py", "pyproject.toml"])
        validation = self.run_skill("mcp-builder", "validate", input="demo-server", options={"language": "python"})
        self.assertTrue(validation["ok"], validation)
        typescript = self.run_skill("mcp-builder", "scaffold", output="demo-ts", options={"language": "typescript"})
        self.assertTrue(typescript["ok"], typescript)
        self.assertEqual(typescript["result"]["files"], ["src/index.ts", "tsconfig.json", "package.json"])
        with open(os.path.join(self.artifacts, "demo-ts", "src", "index.ts"), encoding="utf-8") as stream:
            self.assertIn("z.string()", stream.read())
        ts_validation = self.run_skill("mcp-builder", "validate", input="demo-ts", options={"language": "typescript"})
        self.assertTrue(ts_validation["ok"], ts_validation)
        with open(os.path.join(self.artifacts, "demo-server", "server.py"), "a", encoding="utf-8") as stream:
            stream.write("\nthis is not valid python (\n")
        invalid = self.run_skill("mcp-builder", "validate", input="demo-server", options={"language": "python"})
        self.assertFalse(invalid["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)