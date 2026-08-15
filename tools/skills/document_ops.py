"""Bounded document operations for Eva artifacts and approved workspaces."""

import importlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path


MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARS = 200000
MAX_FORM_FIELDS = 500
MAX_FORM_VALUE_CHARS = 2048
MAX_TABLE_PAGES = 50
MAX_TABLES_PER_PAGE = 20
MAX_TABLE_ROWS = 100
MAX_TABLE_COLUMNS = 50
MAX_TABLE_CELL_CHARS = 1000
MAX_TABLE_RESULT_CHARS = 120000
MAX_OCR_PAGES = 10
MAX_OCR_RESULT_CHARS = 100000
MAX_OCR_PAGE_CHARS = 5000
OCR_TIMEOUT_SECONDS = 60
OFFICE_TIMEOUT_SECONDS = 90
MAX_FORMULA_SCAN_CELLS = 20000
MAX_XLSX_SHEETS = 32
MAX_XLSX_ROWS = 1000
MAX_XLSX_COLUMNS = 100
MAX_XLSX_CELLS = 20000
MAX_XLSX_RESULT_CHARS = 120000
MAX_XLSX_ARCHIVE_ENTRIES = 512
MAX_XLSX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_XLSX_COMPRESSION_RATIO = 100
SUPPORTED_SKILLS = {"docx", "pdf", "pptx", "xlsx", "mcp-builder"}


class SkillExecutionError(Exception):
    """An expected, structured failure from a bounded skill."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _error(code, message, details=None):
    return {"code": code, "message": message, "details": details or {}}


def _display(root_name, path, root):
    try:
        relative = os.path.relpath(path, root).replace(os.sep, "/")
    except ValueError:
        relative = os.path.basename(path)
    return {"root": root_name, "path": relative}


def _receipt(skill, operation, inputs=None, output=None, validation=None, warnings=None, error=None, result=None):
    return {
        "ok": bool(error is None and validation and validation.get("status") == "valid"),
        "skill": skill,
        "operation": operation,
        "input": inputs or [],
        "output": output,
        "validation": validation or {"status": "not-run"},
        "warnings": warnings or [],
        "error": error,
        "result": result or {},
    }


def _dependency(module_name, package_name):
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise SkillExecutionError(
            "missing_dependency",
            "The optional dependency is unavailable; Eva will not install it automatically.",
            {"import": module_name, "package": package_name, "install": "python3 -m pip install " + package_name},
        ) from exc


def _configured_roots(artifacts_dir=None, approved_workspace_roots=None):
    artifact_root = artifacts_dir or os.environ.get(
        "EVA_ARTIFACTS_DIR", os.path.expanduser("~/.config/eva-standalone/artifacts")
    )
    configured = approved_workspace_roots
    if configured is None:
        configured = os.environ.get("EVA_SKILLS_WORKSPACE_ROOTS", "")
    if isinstance(configured, str):
        configured = configured.split(os.pathsep)
    roots = {"artifacts": os.path.abspath(os.path.expanduser(str(artifact_root)))}
    workspace_roots = [os.path.abspath(os.path.expanduser(str(item))) for item in (configured or []) if str(item).strip()]
    if workspace_roots:
        roots["workspace"] = workspace_roots[0]
    return roots


def _safe_relative(value):
    text = str(value or "").strip()
    if not text or len(text) > 512 or "\x00" in text or "\\" in text:
        raise SkillExecutionError("invalid_path", "Paths must be non-empty relative POSIX paths without NUL or backslash characters.")
    path = Path(text)
    if path.is_absolute() or any(part in ("..", "") for part in path.parts):
        raise SkillExecutionError("invalid_path", "Absolute paths and traversal are not allowed.")
    return "/".join(path.parts)


def _reject_symlink_components(root, relative):
    current = root
    for component in relative.split("/"):
        current = os.path.join(current, component)
        if os.path.lexists(current) and os.path.islink(current):
            raise SkillExecutionError("symlink_rejected", "Symlink path components are not allowed.", {"path": relative})


def _resolve_path(value, root_name, roots, kind="input", extension_set=None, directory=False):
    if root_name not in roots:
        if root_name == "workspace":
            raise SkillExecutionError("workspace_root_unavailable", "No trusted approved workspace root is configured.")
        raise SkillExecutionError("invalid_root", "The root must be artifacts or an approved workspace.")
    relative = _safe_relative(value)
    root = roots[root_name]
    if kind == "input" and not os.path.isdir(root):
        raise SkillExecutionError("root_unavailable", "The selected root directory is unavailable.", {"root": root_name})
    if kind == "output" and root_name == "artifacts":
        os.makedirs(root, mode=0o700, exist_ok=True)
    _reject_symlink_components(root, relative)
    candidate = os.path.abspath(os.path.join(root, relative))
    real_root = os.path.realpath(root)
    real_candidate = os.path.realpath(candidate)
    if os.path.commonpath([real_root, real_candidate]) != real_root:
        raise SkillExecutionError("path_escape", "The requested path escapes its approved root.")
    if directory:
        if kind == "input" and not os.path.isdir(candidate):
            raise SkillExecutionError("input_not_found", "The requested project directory was not found.", {"path": relative})
    elif kind == "input":
        if not os.path.isfile(candidate) or os.path.islink(candidate):
            raise SkillExecutionError("input_not_found", "The requested input file was not found as a regular file.", {"path": relative})
        if os.path.getsize(candidate) > MAX_INPUT_BYTES:
            raise SkillExecutionError("input_too_large", "The input exceeds Eva's bounded file size limit.", {"max_bytes": MAX_INPUT_BYTES})
    elif os.path.lexists(candidate) and os.path.islink(candidate):
        raise SkillExecutionError("symlink_rejected", "The output path is a symlink.", {"path": relative})
    if extension_set and not directory and os.path.splitext(relative)[1].lower() not in extension_set:
        raise SkillExecutionError("unsupported_extension", "The file extension is not supported for this operation.", {"allowed": sorted(extension_set)})
    return candidate, _display(root_name, candidate, root)


def _atomic_output(path, writer):
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if os.path.lexists(path) and os.path.islink(path):
        raise SkillExecutionError("symlink_rejected", "The output path is a symlink.")
    fd, temporary = tempfile.mkstemp(prefix=".eva-skill-", dir=parent)
    os.close(fd)
    try:
        writer(temporary)
        with open(temporary, "rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _text(value, default=""):
    text = str(value if value is not None else default)
    if len(text) > MAX_TEXT_CHARS:
        raise SkillExecutionError("input_too_large", "Text content exceeds Eva's bounded text limit.", {"max_chars": MAX_TEXT_CHARS})
    return text


def _dependency_validation(module_name, package_name):
    return _dependency(module_name, package_name)


def _fixed_executable(names, package_name, description):
    for name in names:
        executable = shutil.which(name)
        if executable:
            return executable
    raise SkillExecutionError(
        "missing_dependency",
        description + " is unavailable; install the declared system dependency.",
        {"executables": list(names), "package": package_name, "install": "Install the " + package_name + " system package."},
    )


def _reject_same_output(input_path, output_path):
    if os.path.abspath(input_path) == os.path.abspath(output_path):
        raise SkillExecutionError("invalid_output", "The output must be a new file and must not replace the input.")


def _run_libreoffice_conversion(input_path, output_format, copy_to):
    executable = _fixed_executable(("libreoffice", "soffice"), "libreoffice", "LibreOffice")
    with tempfile.TemporaryDirectory(prefix="eva-office-") as temporary:
        profile = os.path.join(temporary, "profile")
        output_dir = os.path.join(temporary, "output")
        os.makedirs(profile, mode=0o700)
        os.makedirs(output_dir, mode=0o700)
        source_name = "eva-input" + os.path.splitext(input_path)[1].lower()
        source_path = os.path.join(temporary, source_name)
        shutil.copyfile(input_path, source_path)
        command = [
            executable,
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            "-env:UserInstallation=" + Path(profile).as_uri(),
            "--convert-to",
            output_format,
            "--outdir",
            output_dir,
            source_path,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=temporary,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=OFFICE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SkillExecutionError(
                "timeout",
                "LibreOffice did not finish the bounded conversion before the timeout.",
                {"timeout_seconds": OFFICE_TIMEOUT_SECONDS},
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or b"").decode("utf-8", "replace")[:500]
            raise SkillExecutionError(
                "conversion_failed",
                "LibreOffice could not convert the document.",
                {"returncode": completed.returncode, "message": detail},
            )
        converted = os.path.join(output_dir, "eva-input." + output_format)
        if not os.path.isfile(converted):
            raise SkillExecutionError(
                "conversion_failed",
                "LibreOffice reported success but did not produce the expected output.",
                {"format": output_format},
            )
        copy_to(converted)


def _pdf_scalar(value):
    if value is None:
        return None
    try:
        value = value.get_object()
    except AttributeError:
        pass
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")[:MAX_FORM_VALUE_CHARS]
    return str(value)[:MAX_FORM_VALUE_CHARS]


def _form_summary(reader):
    fields = reader.get_fields() or {}
    if len(fields) > MAX_FORM_FIELDS:
        raise SkillExecutionError("result_too_large", "The PDF contains more form fields than Eva can safely inspect.", {"max_fields": MAX_FORM_FIELDS})
    summary = []
    for name, field in fields.items():
        field = field or {}
        summary.append({
            "name": str(name)[:256],
            "type": _pdf_scalar(field.get("/FT")) or "unknown",
            "value": _pdf_scalar(field.get("/V")),
        })
    return {"fields": summary, "field_count": len(summary)}


def _form_values(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        text = "" if value is None else str(value)
        if len(text) > MAX_FORM_VALUE_CHARS:
            raise SkillExecutionError("input_too_large", "Form field values exceed Eva's bounded value limit.", {"max_chars": MAX_FORM_VALUE_CHARS})
        return value
    if isinstance(value, list) and len(value) <= 50 and all(isinstance(item, (str, int, float, bool)) for item in value):
        converted = [str(item) for item in value]
        if sum(len(item) for item in converted) > MAX_FORM_VALUE_CHARS:
            raise SkillExecutionError("input_too_large", "Form field values exceed Eva's bounded value limit.", {"max_chars": MAX_FORM_VALUE_CHARS})
        return value
    raise SkillExecutionError("invalid_input", "Form field values must be scalar values or short arrays.")


def _table_cell(value):
    if value is None:
        return None
    return str(value)[:MAX_TABLE_CELL_CHARS]


def _table_result(page, tables, text, result_chars):
    bounded_tables = []
    truncated = False
    for table in tables[:MAX_TABLES_PER_PAGE]:
        rows = []
        for row in (table or [])[:MAX_TABLE_ROWS]:
            if not isinstance(row, list):
                row = [row]
            rows.append([_table_cell(cell) for cell in row[:MAX_TABLE_COLUMNS]])
        if len(table or []) > MAX_TABLE_ROWS:
            truncated = True
        bounded_tables.append(rows)
    if len(tables) > MAX_TABLES_PER_PAGE:
        truncated = True
    bounded_text = str(text or "")[:MAX_TEXT_CHARS]
    payload = {"page": page, "text": bounded_text, "tables": bounded_tables}
    size = len(json.dumps(payload, ensure_ascii=True, default=str))
    if result_chars + size > MAX_TABLE_RESULT_CHARS:
        remaining = max(0, MAX_TABLE_RESULT_CHARS - result_chars)
        if remaining < 256:
            return None, True
        payload["text"] = bounded_text[:remaining]
        payload["tables"] = []
        truncated = True
    return payload, truncated


def _validate_docx(path):
    module = _dependency_validation("docx", "python-docx")
    document = module.Document(path)
    paragraphs = [paragraph.text for paragraph in document.paragraphs]
    return {"status": "valid", "paragraphs": len(paragraphs), "text_chars": sum(len(item) for item in paragraphs)}


def _validate_pdf(path):
    module = _dependency_validation("pypdf", "pypdf")
    reader = module.PdfReader(path, strict=False)
    return {"status": "valid", "pages": len(reader.pages), "text_chars": sum(len(page.extract_text() or "") for page in reader.pages)}


def _validate_pptx(path):
    module = _dependency_validation("pptx", "python-pptx")
    presentation = module.Presentation(path)
    return {"status": "valid", "slides": len(presentation.slides), "text_chars": sum(len(shape.text) for slide in presentation.slides for shape in slide.shapes if getattr(shape, "has_text_frame", False))}


def _preflight_xlsx_archive(path):
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise SkillExecutionError("invalid_input", "The XLSX archive could not be inspected.", {"type": type(exc).__name__}) from exc
    if len(entries) > MAX_XLSX_ARCHIVE_ENTRIES:
        raise SkillExecutionError("input_too_large", "The XLSX archive has too many entries.", {"max_entries": MAX_XLSX_ARCHIVE_ENTRIES})
    uncompressed = sum(entry.file_size for entry in entries)
    compressed = sum(entry.compress_size for entry in entries)
    if uncompressed > MAX_XLSX_UNCOMPRESSED_BYTES:
        raise SkillExecutionError("input_too_large", "The XLSX archive expands beyond Eva's fixed limit.", {"max_uncompressed_bytes": MAX_XLSX_UNCOMPRESSED_BYTES})
    if uncompressed and uncompressed > max(1, compressed) * MAX_XLSX_COMPRESSION_RATIO:
        raise SkillExecutionError("input_too_large", "The XLSX archive compression ratio exceeds Eva's fixed limit.", {"max_ratio": MAX_XLSX_COMPRESSION_RATIO})


def _formula_summary(workbook):
    formulas = []
    invalid = []
    if len(workbook.worksheets) > MAX_XLSX_SHEETS:
        raise SkillExecutionError("result_too_large", "The workbook has too many sheets to validate safely.", {"max_sheets": MAX_XLSX_SHEETS})
    scanned = 0
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                scanned += 1
                if scanned > MAX_XLSX_CELLS:
                    raise SkillExecutionError("result_too_large", "The workbook exceeds Eva's bounded validation scan.", {"max_cells": MAX_XLSX_CELLS})
                value = cell.value
                if isinstance(value, str) and value.startswith("="):
                    formulas.append(worksheet.title + "!" + cell.coordinate)
                    if value.count("(") != value.count(")") or "\n" in value:
                        invalid.append(worksheet.title + "!" + cell.coordinate)
    return formulas, invalid


def _validate_xlsx(path):
    module = _dependency_validation("openpyxl", "openpyxl")
    _preflight_xlsx_archive(path)
    workbook = module.load_workbook(path, data_only=False, read_only=True)
    try:
        formulas, invalid = _formula_summary(workbook)
        if invalid:
            raise SkillExecutionError("invalid_formula", "Formula validation failed for one or more cells.", {"cells": invalid})
        return {"status": "valid", "sheets": workbook.sheetnames, "formulas": formulas, "formula_count": len(formulas)}
    finally:
        workbook.close()


def _recalculated_formula_summary(path):
    module = _dependency_validation("openpyxl", "openpyxl")
    _preflight_xlsx_archive(path)
    formulas_workbook = module.load_workbook(path, data_only=False, read_only=False)
    cached_workbook = module.load_workbook(path, data_only=True, read_only=False)
    formulas = []
    errors = []
    cached_results = []
    scanned = 0
    try:
        for worksheet in formulas_workbook.worksheets:
            if len(formulas_workbook.worksheets) > MAX_XLSX_SHEETS:
                raise SkillExecutionError("result_too_large", "The workbook has too many sheets to inspect safely.", {"max_sheets": MAX_XLSX_SHEETS})
            cached_sheet = cached_workbook[worksheet.title]
            for row in worksheet.iter_rows():
                for cell in row:
                    scanned += 1
                    if scanned > MAX_FORMULA_SCAN_CELLS:
                        raise SkillExecutionError("result_too_large", "The workbook exceeds Eva's bounded formula scan.", {"max_cells": MAX_FORMULA_SCAN_CELLS})
                    value = cell.value
                    if not (isinstance(value, str) and value.startswith("=")):
                        continue
                    reference = worksheet.title + "!" + cell.coordinate
                    cached = cached_sheet[cell.coordinate].value
                    formulas.append(reference)
                    if isinstance(cached, str) and cached.startswith("#"):
                        errors.append({"cell": reference, "error": cached[:100]})
                    if cached is not None:
                        if not isinstance(cached, (str, int, float, bool)):
                            cached = str(cached)[:MAX_FORM_VALUE_CHARS]
                        cached_results.append({"cell": reference, "value": cached})
    finally:
        formulas_workbook.close()
        cached_workbook.close()
    return {"formulas": formulas, "formula_count": len(formulas), "formula_errors": errors, "cached_results": cached_results}


def _recalculate_xlsx(input_path, output_path):
    _reject_same_output(input_path, output_path)
    _atomic_output(output_path, lambda path: _run_libreoffice_conversion(input_path, "xlsx", lambda converted: shutil.copyfile(converted, path)))


def _docx_operation(operation, request, roots):
    module = _dependency_validation("docx", "python-docx")
    options = request.get("options") or {}
    root_name = str(request.get("root") or "artifacts").lower()
    inputs = []
    if operation in ("read", "validate"):
        path, display = _resolve_path(request.get("input"), root_name, roots, extension_set={".docx"})
        validation = _validate_docx(path) if operation == "validate" else {"status": "valid"}
        document = module.Document(path)
        paragraphs = [paragraph.text for paragraph in document.paragraphs]
        return _receipt("docx", operation, [display], None, validation, result={"text": "\n".join(paragraphs)[:MAX_TEXT_CHARS], "paragraphs": len(paragraphs), "metadata": {"title": document.core_properties.title, "author": document.core_properties.author}})
    if operation == "render":
        input_path, input_display = _resolve_path(request.get("input"), root_name, roots, extension_set={".docx"})
        output_path, output = _resolve_path(request.get("output"), root_name, roots, kind="output", extension_set={".pdf"})
        _reject_same_output(input_path, output_path)
        _atomic_output(output_path, lambda path: _run_libreoffice_conversion(input_path, "pdf", lambda converted: shutil.copyfile(converted, path)))
        validation = _validate_pdf(output_path)
        return _receipt("docx", operation, [input_display], output, validation, result={"message": "DOCX rendered to PDF and reopened successfully."})
    input_path = None
    inputs = []
    if operation == "edit":
        input_path, input_display = _resolve_path(request.get("input"), root_name, roots, extension_set={".docx"})
        inputs = [input_display]
    output_path, output = _resolve_path(request.get("output"), root_name, roots, kind="output", extension_set={".docx"})

    def write(path):
        document = module.Document(input_path) if input_path else module.Document()
        if input_path:
            replacement = options.get("replace") or {}
            if replacement:
                find = _text(replacement.get("find"))
                replace = _text(replacement.get("replace"))
                for paragraph in document.paragraphs:
                    if find:
                        paragraph.text = paragraph.text.replace(find, replace)
                for table in document.tables:
                    for row in table.rows:
                        for cell in row.cells:
                            for paragraph in cell.paragraphs:
                                paragraph.text = paragraph.text.replace(find, replace)
            append_text = _text(options.get("append_text"))
            if append_text:
                document.add_paragraph(append_text)
        else:
            for paragraph in _text(options.get("text", options.get("content", ""))).splitlines() or [""]:
                document.add_paragraph(paragraph)
        metadata = options.get("metadata") or {}
        for key in ("title", "subject", "author", "keywords", "comments"):
            if key in metadata:
                setattr(document.core_properties, key, _text(metadata[key]))
        document.save(path)

    _atomic_output(output_path, write)
    validation = _validate_docx(output_path)
    return _receipt("docx", operation, inputs, output, validation, result={"message": "DOCX written and reopened successfully."})


def _pdf_operation(operation, request, roots):
    options = request.get("options") or {}
    root_name = str(request.get("root") or "artifacts").lower()
    if operation == "inspect-form":
        module = _dependency_validation("pypdf", "pypdf")
        input_path, input_display = _resolve_path(request.get("input"), root_name, roots, extension_set={".pdf"})
        reader = module.PdfReader(input_path, strict=False)
        summary = _form_summary(reader)
        return _receipt("pdf", operation, [input_display], None, {"status": "valid", "pages": len(reader.pages), "field_count": summary["field_count"]}, result=summary)
    if operation == "fill-form":
        module = _dependency_validation("pypdf", "pypdf")
        input_path, input_display = _resolve_path(request.get("input"), root_name, roots, extension_set={".pdf"})
        output_path, output = _resolve_path(request.get("output"), root_name, roots, kind="output", extension_set={".pdf"})
        _reject_same_output(input_path, output_path)
        reader = module.PdfReader(input_path, strict=False)
        before = _form_summary(reader)
        field_values = options.get("fields", request.get("fields"))
        if not isinstance(field_values, dict) or not field_values:
            raise SkillExecutionError("invalid_input", "PDF fill-form requires a non-empty options.fields object.")
        if len(field_values) > MAX_FORM_FIELDS:
            raise SkillExecutionError("input_too_large", "The form field update set is too large.", {"max_fields": MAX_FORM_FIELDS})
        known = {item["name"] for item in before["fields"]}
        unknown = sorted(str(name) for name in field_values if str(name) not in known)
        allow_unknown = bool(options.get("allow_unknown", False))
        if unknown and not allow_unknown:
            raise SkillExecutionError("unknown_field", "The form contains unknown field names.", {"fields": unknown[:MAX_FORM_FIELDS]})
        values = {str(name): _form_values(value) for name, value in field_values.items() if str(name) in known}

        def write(path):
            writer = module.PdfWriter()
            writer.clone_document_from_reader(reader)
            for page in writer.pages:
                writer.update_page_form_field_values(page, values, auto_regenerate=False)
            with open(path, "wb") as stream:
                writer.write(stream)

        _atomic_output(output_path, write)
        reopened = module.PdfReader(output_path, strict=False)
        after = _form_summary(reopened)
        filled = [item for item in after["fields"] if item["name"] in values]
        validation = {"status": "valid", "pages": len(reopened.pages), "field_count": after["field_count"]}
        warnings = ["Unknown field names were ignored because options.allow_unknown was true."] if unknown else []
        return _receipt("pdf", operation, [input_display], output, validation, warnings=warnings, result={"fields": after["fields"], "field_count": after["field_count"], "filled_fields": filled, "unknown_fields": unknown})
    if operation == "tables":
        module = _dependency_validation("pdfplumber", "pdfplumber")
        input_path, input_display = _resolve_path(request.get("input"), root_name, roots, extension_set={".pdf"})
        pages = []
        result_chars = 0
        truncated = False
        with module.open(input_path) as document:
            if len(document.pages) > MAX_TABLE_PAGES:
                raise SkillExecutionError("input_too_large", "The PDF has more pages than Eva can safely inspect for tables.", {"max_pages": MAX_TABLE_PAGES})
            for index, page in enumerate(document.pages, 1):
                payload, page_truncated = _table_result(index, page.extract_tables() or [], page.extract_text() or "", result_chars)
                if payload is None:
                    truncated = True
                    break
                pages.append(payload)
                result_chars += len(json.dumps(payload, ensure_ascii=True, default=str))
                truncated = truncated or page_truncated
        warnings = ["Table and text results were bounded and truncated."] if truncated else []
        return _receipt("pdf", operation, [input_display], None, {"status": "valid", "pages": len(pages)}, warnings=warnings, result={"pages": pages, "page_count": len(pages), "truncated": truncated})
    if operation == "ocr":
        pypdf = _dependency_validation("pypdf", "pypdf")
        pytesseract = _dependency_validation("pytesseract", "pytesseract")
        image_module = _dependency_validation("PIL.Image", "Pillow")
        pdftoppm = _fixed_executable(("pdftoppm",), "poppler-utils", "Poppler pdftoppm")
        tesseract = _fixed_executable(("tesseract",), "tesseract-ocr", "Tesseract OCR")
        input_path, input_display = _resolve_path(request.get("input"), root_name, roots, extension_set={".pdf"})
        output_path = None
        output = None
        if request.get("output") is not None and str(request.get("output")).strip():
            output_path, output = _resolve_path(request.get("output"), root_name, roots, kind="output", extension_set={".txt"})
        reader = pypdf.PdfReader(input_path, strict=False)
        page_count = len(reader.pages)
        try:
            page_start = int(options.get("page_start", 1))
            page_end = int(options.get("page_end", min(page_count, MAX_OCR_PAGES)))
        except (TypeError, ValueError) as exc:
            raise SkillExecutionError("invalid_input", "OCR page_start and page_end must be integers.") from exc
        if page_start < 1 or page_end < page_start or page_end > page_count or page_end - page_start + 1 > MAX_OCR_PAGES:
            raise SkillExecutionError("invalid_page", "OCR pages are outside the document or exceed Eva's fixed page bound.", {"pages": page_count, "max_pages": MAX_OCR_PAGES})
        deadline = time.monotonic() + OCR_TIMEOUT_SECONDS
        previous_tesseract = getattr(pytesseract.pytesseract, "tesseract_cmd", "tesseract")
        page_results = []
        ocr_text_chars = 0
        truncated = False
        try:
            with tempfile.TemporaryDirectory(prefix="eva-ocr-") as temporary:
                os.chmod(temporary, 0o700)
                prefix = os.path.join(temporary, "page")
                remaining = max(0.1, deadline - time.monotonic())
                try:
                    rendered = subprocess.run(
                        [pdftoppm, "-f", str(page_start), "-l", str(page_end), "-r", "150", "-png", input_path, prefix],
                        cwd=temporary,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=remaining,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise SkillExecutionError("timeout", "Poppler did not finish bounded PDF rendering before the OCR timeout.", {"timeout_seconds": OCR_TIMEOUT_SECONDS}) from exc
                if rendered.returncode != 0:
                    detail = (rendered.stderr or b"").decode("utf-8", "replace")[:500]
                    raise SkillExecutionError("ocr_failed", "Poppler could not render the PDF for OCR.", {"returncode": rendered.returncode, "message": detail})
                images = sorted(Path(temporary).glob("page-*.png"))
                if len(images) != page_end - page_start + 1:
                    raise SkillExecutionError("ocr_failed", "Poppler did not produce the expected bounded page images.")
                pytesseract.pytesseract.tesseract_cmd = tesseract
                for offset, image_path in enumerate(images):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SkillExecutionError("timeout", "OCR exceeded Eva's fixed time limit.", {"timeout_seconds": OCR_TIMEOUT_SECONDS})
                    try:
                        with image_module.open(image_path) as image:
                            text = pytesseract.image_to_string(image, timeout=max(0.1, remaining))
                    except (RuntimeError, subprocess.TimeoutExpired) as exc:
                        raise SkillExecutionError("timeout", "Tesseract OCR exceeded Eva's fixed time limit.", {"timeout_seconds": OCR_TIMEOUT_SECONDS}) from exc
                    text = str(text or "")
                    remaining_chars = max(0, MAX_OCR_RESULT_CHARS // 2 - ocr_text_chars)
                    bounded_text = text[:min(MAX_OCR_PAGE_CHARS, remaining_chars)]
                    truncated = truncated or len(text) > len(bounded_text)
                    page_results.append({"page": page_start + offset, "text": bounded_text})
                    ocr_text_chars += len(bounded_text)
        finally:
            pytesseract.pytesseract.tesseract_cmd = previous_tesseract
        full_text = "\n\n".join(item["text"] for item in page_results)
        truncated = truncated or len(full_text) > MAX_OCR_RESULT_CHARS // 2
        full_text = full_text[:MAX_OCR_RESULT_CHARS // 2]
        warnings = ["OCR text was bounded and truncated."] if truncated else []
        if output_path:
            def write_text(path):
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write(full_text)

            _atomic_output(output_path, write_text)
        return _receipt("pdf", operation, [input_display], output, {"status": "valid", "pages": page_end - page_start + 1, "text_chars": len(full_text)}, warnings=warnings, result={"pages": page_results, "text": full_text, "text_chars": len(full_text), "truncated": truncated})
    if operation in ("read", "validate"):
        path, display = _resolve_path(request.get("input"), root_name, roots, extension_set={".pdf"})
        validation = _validate_pdf(path)
        if operation == "validate":
            return _receipt("pdf", operation, [display], None, validation)
        module = _dependency_validation("pypdf", "pypdf")
        reader = module.PdfReader(path, strict=False)
        return _receipt("pdf", operation, [display], None, validation, result={"pages": len(reader.pages), "text": "\n".join(page.extract_text() or "" for page in reader.pages)[:MAX_TEXT_CHARS]})
    if operation == "create":
        output_path, output = _resolve_path(request.get("output"), root_name, roots, kind="output", extension_set={".pdf"})
        reportlab = _dependency_validation("reportlab.pdfgen.canvas", "reportlab")
        pagesizes = _dependency_validation("reportlab.lib.pagesizes", "reportlab")

        def write(path):
            canvas = reportlab.Canvas(path, pagesize=pagesizes.letter)
            y = 750
            for line in _text(options.get("text", options.get("content", ""))).splitlines() or [""]:
                canvas.drawString(50, y, line[:110])
                y -= 16
                if y < 50:
                    canvas.showPage()
                    y = 750
            canvas.save()

        _atomic_output(output_path, write)
        return _receipt("pdf", operation, [], output, _validate_pdf(output_path), result={"message": "PDF written and reopened successfully."})
    if operation == "merge":
        raw_inputs = request.get("inputs") or request.get("input")
        if not isinstance(raw_inputs, list) or not raw_inputs:
            raise SkillExecutionError("invalid_input", "PDF merge requires a non-empty inputs list.")
        module = _dependency_validation("pypdf", "pypdf")
        paths = []
        displays = []
        for item in raw_inputs:
            path, display = _resolve_path(item, root_name, roots, extension_set={".pdf"})
            paths.append(path)
            displays.append(display)
        output_path, output = _resolve_path(request.get("output"), root_name, roots, kind="output", extension_set={".pdf"})

        def write(path):
            writer = module.PdfWriter()
            for source in paths:
                writer.append(source)
            with open(path, "wb") as stream:
                writer.write(stream)

        _atomic_output(output_path, write)
        return _receipt("pdf", operation, displays, output, _validate_pdf(output_path), result={"message": "PDFs merged and reopened successfully."})
    if operation == "split":
        module = _dependency_validation("pypdf", "pypdf")
        input_path, input_display = _resolve_path(request.get("input"), root_name, roots, extension_set={".pdf"})
        output_path, output = _resolve_path(request.get("output"), root_name, roots, kind="output", extension_set={".pdf"})
        reader = module.PdfReader(input_path, strict=False)
        page_number = int(options.get("page", 1))
        if page_number < 1 or page_number > len(reader.pages):
            raise SkillExecutionError("invalid_page", "The requested PDF page is outside the document.", {"pages": len(reader.pages)})

        def write(path):
            writer = module.PdfWriter()
            writer.add_page(reader.pages[page_number - 1])
            with open(path, "wb") as stream:
                writer.write(stream)

        _atomic_output(output_path, write)
        return _receipt("pdf", operation, [input_display], output, _validate_pdf(output_path), result={"page": page_number})
    if operation == "extract":
        module = _dependency_validation("pypdf", "pypdf")
        input_path, input_display = _resolve_path(request.get("input"), root_name, roots, extension_set={".pdf"})
        output_path, output = _resolve_path(request.get("output"), root_name, roots, kind="output", extension_set={".txt"})
        reader = module.PdfReader(input_path, strict=False)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)[:MAX_TEXT_CHARS]

        def write(path):
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(text)

        _atomic_output(output_path, write)
        return _receipt("pdf", operation, [input_display], output, {"status": "valid", "text_chars": len(text)}, result={"text_chars": len(text)})
    raise SkillExecutionError("unsupported_operation", "Unsupported PDF operation.")


def _pptx_operation(operation, request, roots):
    module = _dependency_validation("pptx", "python-pptx")
    options = request.get("options") or {}
    root_name = str(request.get("root") or "artifacts").lower()
    if operation in ("read", "validate"):
        path, display = _resolve_path(request.get("input"), root_name, roots, extension_set={".pptx"})
        validation = _validate_pptx(path)
        if operation == "validate":
            return _receipt("pptx", operation, [display], None, validation)
        presentation = module.Presentation(path)
        text = []
        for slide in presentation.slides:
            text.append("\n".join(shape.text for shape in slide.shapes if getattr(shape, "has_text_frame", False)))
        return _receipt("pptx", operation, [display], None, validation, result={"text": "\n\n".join(text)[:MAX_TEXT_CHARS]})
    if operation == "render":
        input_path, input_display = _resolve_path(request.get("input"), root_name, roots, extension_set={".pptx"})
        output_path, output = _resolve_path(request.get("output"), root_name, roots, kind="output", extension_set={".pdf"})
        _reject_same_output(input_path, output_path)
        _atomic_output(output_path, lambda path: _run_libreoffice_conversion(input_path, "pdf", lambda converted: shutil.copyfile(converted, path)))
        validation = _validate_pdf(output_path)
        return _receipt("pptx", operation, [input_display], output, validation, result={"message": "PPTX rendered to PDF and reopened successfully."})
    input_path = None
    inputs = []
    if operation == "edit":
        input_path, display = _resolve_path(request.get("input"), root_name, roots, extension_set={".pptx"})
        inputs = [display]
    output_path, output = _resolve_path(request.get("output"), root_name, roots, kind="output", extension_set={".pptx"})

    def write(path):
        presentation = module.Presentation(input_path) if input_path else module.Presentation()
        if input_path:
            replacement = options.get("replace") or {}
            find = _text(replacement.get("find"))
            replace = _text(replacement.get("replace"))
            for slide in presentation.slides:
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False) and find:
                        shape.text = shape.text.replace(find, replace)
        else:
            slide = presentation.slides.add_slide(presentation.slide_layouts[1])
            slide.shapes.title.text = _text(options.get("title", "Eva presentation"))
            slide.placeholders[1].text = _text(options.get("body", options.get("text", "")))
        presentation.save(path)

    _atomic_output(output_path, write)
    return _receipt("pptx", operation, inputs, output, _validate_pptx(output_path), result={"message": "PPTX written and reopened successfully."})


def _xlsx_operation(operation, request, roots):
    options = request.get("options") or {}
    root_name = str(request.get("root") or "artifacts").lower()
    if operation == "recalculate":
        input_path, input_display = _resolve_path(request.get("input"), root_name, roots, extension_set={".xlsx"})
        output_path, output = _resolve_path(request.get("output"), root_name, roots, kind="output", extension_set={".xlsx"})
        _preflight_xlsx_archive(input_path)
        _recalculate_xlsx(input_path, output_path)
        validation = _validate_xlsx(output_path)
        recalculated = _recalculated_formula_summary(output_path)
        validation.update({"formula_count": recalculated["formula_count"], "formula_errors": recalculated["formula_errors"], "cached_results": recalculated["cached_results"]})
        if recalculated["formula_errors"]:
            return _receipt(
                "xlsx",
                operation,
                [input_display],
                output,
                dict(validation, status="invalid"),
                error=_error("formula_error", "Recalculated workbook contains cached formula errors.", {"cells": recalculated["formula_errors"]}),
                result=recalculated,
            )
        warnings = []
        if recalculated["formula_count"] and len(recalculated["cached_results"]) < recalculated["formula_count"]:
            warnings.append("LibreOffice completed, but one or more formula cells did not expose a cached result to openpyxl.")
        return _receipt("xlsx", operation, [input_display], output, validation, warnings=warnings, result={"message": "XLSX recalculated by LibreOffice and reopened successfully.", **recalculated})
    if operation in ("read", "validate"):
        path, display = _resolve_path(request.get("input"), root_name, roots, extension_set={".xlsx"})
        _preflight_xlsx_archive(path)
        module = _dependency_validation("openpyxl", "openpyxl")
        validation = _validate_xlsx(path)
        if operation == "validate":
            return _receipt("xlsx", operation, [display], None, validation)
        workbook = module.load_workbook(path, data_only=False, read_only=True)
        rows = {}
        if len(workbook.worksheets) > MAX_XLSX_SHEETS:
            workbook.close()
            raise SkillExecutionError("result_too_large", "The workbook has too many sheets to return safely.", {"max_sheets": MAX_XLSX_SHEETS})
        result_chars = 0
        total_cells = 0
        try:
            for worksheet in workbook.worksheets:
                sheet_rows = []
                for row_index, row in enumerate(worksheet.iter_rows(), 1):
                    if row_index > MAX_XLSX_ROWS or len(row) > MAX_XLSX_COLUMNS:
                        raise SkillExecutionError("result_too_large", "The worksheet exceeds Eva's bounded row or column limit.", {"max_rows": MAX_XLSX_ROWS, "max_columns": MAX_XLSX_COLUMNS})
                    values = [cell.value for cell in row]
                    total_cells += len(values)
                    result_chars += sum(len(str(value or "")) for value in values)
                    if total_cells > MAX_XLSX_CELLS or result_chars > MAX_XLSX_RESULT_CHARS:
                        raise SkillExecutionError("result_too_large", "The workbook exceeds Eva's bounded read result.", {"max_cells": MAX_XLSX_CELLS, "max_chars": MAX_XLSX_RESULT_CHARS})
                    sheet_rows.append(values)
                rows[worksheet.title] = sheet_rows
        finally:
            workbook.close()
        return _receipt("xlsx", operation, [display], None, validation, result={"sheets": rows, "formulas": validation.get("formulas", [])})
    input_path = None
    inputs = []
    if operation == "edit":
        input_path, display = _resolve_path(request.get("input"), root_name, roots, extension_set={".xlsx"})
        _preflight_xlsx_archive(input_path)
        inputs = [display]
    output_path, output = _resolve_path(request.get("output"), root_name, roots, kind="output", extension_set={".xlsx"})
    module = _dependency_validation("openpyxl", "openpyxl")

    def write(path):
        workbook = module.load_workbook(input_path) if input_path else module.Workbook()
        sheet_name = str(options.get("sheet") or workbook.active.title)
        worksheet = workbook[sheet_name] if sheet_name in workbook.sheetnames else workbook.create_sheet(sheet_name)
        if not input_path:
            write_rows = options.get("rows", [])
            if not isinstance(write_rows, list) or len(write_rows) > MAX_XLSX_ROWS:
                raise SkillExecutionError("input_too_large", "XLSX rows exceed Eva's bounded input limit.", {"max_rows": MAX_XLSX_ROWS})
            total_cells = 0
            for row in write_rows:
                if not isinstance(row, list):
                    raise SkillExecutionError("invalid_input", "XLSX rows must be arrays.")
                if len(row) > MAX_XLSX_COLUMNS:
                    raise SkillExecutionError("input_too_large", "XLSX rows exceed Eva's bounded column limit.", {"max_columns": MAX_XLSX_COLUMNS})
                total_cells += len(row)
                if total_cells > MAX_XLSX_CELLS:
                    raise SkillExecutionError("input_too_large", "XLSX rows exceed Eva's bounded cell limit.", {"max_cells": MAX_XLSX_CELLS})
                worksheet.append(row)
        cells = options.get("cells") or {}
        if not isinstance(cells, dict) or len(cells) > MAX_XLSX_CELLS:
            raise SkillExecutionError("input_too_large", "XLSX cell updates exceed Eva's bounded input limit.", {"max_cells": MAX_XLSX_CELLS})
        for coordinate, value in cells.items():
            if not re.fullmatch(r"[A-Za-z]{1,3}[1-9][0-9]*", str(coordinate)):
                raise SkillExecutionError("invalid_input", "XLSX cell coordinates must look like A1.", {"cell": coordinate})
            worksheet[str(coordinate)] = value
        workbook.save(path)

    _atomic_output(output_path, write)
    validation = _validate_xlsx(output_path)
    return _receipt("xlsx", operation, inputs, output, validation, result={"message": "XLSX written and reopened successfully."})


def execute_bounded_skill(request, artifacts_dir=None, approved_workspace_roots=None):
    """Execute one fixed skill operation and always return an action receipt."""
    request = request if isinstance(request, dict) else {}
    skill = str(request.get("skill") or "").strip().lower()
    operation = str(request.get("operation") or "").strip().lower()
    roots = _configured_roots(artifacts_dir, approved_workspace_roots)
    if skill not in {"docx", "pdf", "pptx", "xlsx", "mcp-builder"}:
        return _receipt(skill, operation, error=_error("unsupported_skill", "Only docx, pdf, pptx, xlsx, and mcp-builder are supported."))
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", operation):
        return _receipt(skill, operation, error=_error("invalid_operation", "Operation name is invalid."))
    supported_operations = {
        "docx": {"create", "read", "validate", "edit", "render"},
        "pdf": {"create", "read", "validate", "merge", "split", "extract", "inspect-form", "fill-form", "tables", "ocr"},
        "pptx": {"create", "read", "validate", "edit", "render"},
        "xlsx": {"create", "read", "validate", "edit", "recalculate"},
        "mcp-builder": {"scaffold", "validate"},
    }
    if operation not in supported_operations[skill]:
        return _receipt(skill, operation, error=_error("unsupported_operation", "The requested operation is not supported by this bounded skill.", {"allowed": sorted(supported_operations[skill])}))
    try:
        if skill == "mcp-builder":
            from .mcp_builder import execute_mcp_builder

            return execute_mcp_builder(request, roots)
        operation_map = {"docx": _docx_operation, "pdf": _pdf_operation, "pptx": _pptx_operation, "xlsx": _xlsx_operation}
        return operation_map[skill](operation, request, roots)
    except SkillExecutionError as exc:
        return _receipt(skill, operation, error=_error(exc.code, exc.message, exc.details))
    except Exception as exc:
        return _receipt(skill, operation, error=_error("operation_failed", "The bounded operation failed before validation.", {"type": type(exc).__name__, "message": str(exc)[:500]}))