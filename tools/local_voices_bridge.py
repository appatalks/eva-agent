#!/usr/bin/env python3
"""Loopback HTTP bridge between Eva's browser UI and the local voices engine."""

from __future__ import annotations

import argparse
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Any


MAX_INPUT_CHARS = 12_000
DEFAULT_REFERENCE_AUDIO = Path(__file__).resolve().parent.parent / "core" / "audio" / "eva-voice.wav"


class LocalVoicesService:
    """Lazily construct and retain one local speech engine for the bridge lifetime."""

    def __init__(self) -> None:
        self._engine: Any | None = None
        self._engine_class: Any | None = None
        self._load_error: str | None = None
        self._synthesis_lock = Lock()

    def reference_audio(self) -> Path:
        configured = os.getenv("LOCAL_VOICES_REFERENCE", "").strip()
        return Path(configured).expanduser() if configured else DEFAULT_REFERENCE_AUDIO

    def engine_class(self) -> Any | None:
        if self._engine_class is not None or self._load_error is not None:
            return self._engine_class
        try:
            from voice_clone_module import VoiceCloner as LocalVoicesEngine

            self._engine_class = LocalVoicesEngine
        except Exception:
            self._load_error = "Local Voices backend is unavailable in this Python environment."
        return self._engine_class

    def health(self) -> dict[str, object]:
        reference = self.reference_audio()
        backend = self.engine_class()
        return {
            "ok": True,
            "engine_loaded": self._engine is not None,
            "backend_available": backend is not None,
            "backend_error": self._load_error,
            "reference_source": "environment" if os.getenv("LOCAL_VOICES_REFERENCE", "").strip() else "bundled",
            "reference_readable": reference.is_file(),
            "load_error": self._load_error,
        }

    def synthesize(self, text: str) -> bytes:
        if not text.strip():
            raise ValueError("input must not be empty")
        if len(text) > MAX_INPUT_CHARS:
            raise ValueError(f"input must be {MAX_INPUT_CHARS} characters or fewer")

        reference = self.reference_audio()
        if not reference.is_file():
            raise RuntimeError("the configured local voice reference is unavailable")

        with self._synthesis_lock:
            if self._engine is None:
                engine_class = self.engine_class()
                if engine_class is None:
                    raise RuntimeError(self._load_error or "Local Voices backend is unavailable")
                try:
                    self._engine = engine_class(
                        reference_audio=reference,
                        device=os.getenv("LOCAL_VOICES_DEVICE", "auto"),
                        exaggeration=float(os.getenv("LOCAL_VOICES_EXAGGERATION", "0.5")),
                        cfg_weight=float(os.getenv("LOCAL_VOICES_CFG_WEIGHT", "0.5")),
                    )
                    self._load_error = None
                except Exception as error:
                    self._load_error = str(error)
                    raise RuntimeError("the local voices engine could not be loaded") from error

            with NamedTemporaryFile(suffix=".wav", delete=False) as output:
                output_path = Path(output.name)
            try:
                self._engine.save(text, output_path)
                return output_path.read_bytes()
            finally:
                output_path.unlink(missing_ok=True)


class LocalVoicesRequestHandler(BaseHTTPRequestHandler):
    service: LocalVoicesService

    def _write_headers(self, status: HTTPStatus, content_type: str, content_length: int = 0) -> None:
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.end_headers()

    def _write_json(self, status: HTTPStatus, data: dict[str, object]) -> None:
        body = json.dumps(data).encode("utf-8")
        self._write_headers(status, "application/json", len(body))
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._write_headers(HTTPStatus.NO_CONTENT, "text/plain")

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/health":
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._write_json(HTTPStatus.OK, self.service.health())

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/speech":
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_INPUT_CHARS + 512:
                raise ValueError("request body has an invalid size")
            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict) or not isinstance(payload.get("input"), str):
                raise ValueError("request body must contain a string input")
            audio = self.service.synthesize(payload["input"])
        except ValueError as error:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except RuntimeError as error:
            self._write_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            return
        except Exception:
            self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "voice synthesis failed"})
            return

        self._write_headers(HTTPStatus.OK, "audio/wav", len(audio))
        self.wfile.write(audio)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int, service: LocalVoicesService | None = None) -> ThreadingHTTPServer:
    handler = type("ConfiguredLocalVoicesRequestHandler", (LocalVoicesRequestHandler,), {})
    handler.service = service or LocalVoicesService()
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Eva's local voices bridge.")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback address to bind (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8090, help="Loopback port to bind (default: 8090).")
    parser.add_argument("--reference", type=Path, help="Reference WAV file for this bridge process.")
    args = parser.parse_args()
    if args.reference:
        os.environ["LOCAL_VOICES_REFERENCE"] = str(args.reference.expanduser())
    server = create_server(args.host, args.port)
    print(f"Eva Local Voices bridge listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()