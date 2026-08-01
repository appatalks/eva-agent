#!/usr/bin/env python3
"""Loopback HTTP bridge between Eva's browser UI and the local voices engine."""

from __future__ import annotations

import argparse
import hmac
import importlib.util
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Any


MAX_INPUT_CHARS = 12_000
MAX_AUDIO_BYTES = 16 * 1024 * 1024
MAX_TRANSCRIPTION_SECONDS = 120
ALLOWED_AUDIO_TYPES = {"audio/webm", "video/webm", "audio/ogg", "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp4"}


class LocalVoicesService:
    """Lazily construct and retain one local speech engine for the bridge lifetime."""

    def __init__(self) -> None:
        self._engine: Any | None = None
        self._engine_class: Any | None = None
        self._load_error: str | None = None
        self._synthesis_lock = Lock()

    def reference_audio(self) -> Path:
        configured = os.getenv("LOCAL_VOICES_REFERENCE", "").strip()
        return Path(configured).expanduser() if configured else Path()

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
            "reference_source": "environment" if os.getenv("LOCAL_VOICES_REFERENCE", "").strip() else "none",
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


class LocalTranscriptionService:
    """Lazily retain a local Faster Whisper model with Silero VAD filtering."""

    def __init__(self) -> None:
        self._model: Any | None = None
        self._load_error: str | None = None
        self._lock = Lock()

    def _settings(self) -> tuple[str, str, str]:
        model = os.getenv("LOCAL_STT_MODEL", "small.en").strip() or "small.en"
        device = os.getenv("LOCAL_STT_DEVICE", "cpu").strip() or "cpu"
        compute_type = os.getenv("LOCAL_STT_COMPUTE_TYPE", "int8").strip() or "int8"
        return model, device, compute_type

    def model(self) -> Any | None:
        if self._model is not None or self._load_error is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel

            model, device, compute_type = self._settings()
            self._model = WhisperModel(model, device=device, compute_type=compute_type)
        except Exception as error:
            self._load_error = str(error)
        return self._model

    def health(self) -> dict[str, object]:
        model, device, compute_type = self._settings()
        available = importlib.util.find_spec("faster_whisper") is not None
        if not available and self._load_error is None:
            self._load_error = "Local transcription backend is unavailable in this Python environment."
        return {
            "available": available,
            "loaded": self._model is not None,
            "model": model,
            "device": device,
            "compute_type": compute_type,
            "error": self._load_error,
            "vad": "silero",
        }

    def transcribe(self, audio: bytes, suffix: str) -> str:
        if not audio:
            raise ValueError("audio input must not be empty")
        if len(audio) > MAX_AUDIO_BYTES:
            raise ValueError("audio input exceeds the maximum size")

        with self._lock:
            model = self.model()
            if model is None:
                raise RuntimeError(self._load_error or "Local transcription backend is unavailable")
            with NamedTemporaryFile(suffix=suffix, delete=False) as source:
                source.write(audio)
                source_path = Path(source.name)
            try:
                segments, info = model.transcribe(
                    str(source_path),
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 500},
                    condition_on_previous_text=False,
                )
                if getattr(info, "duration", 0) > MAX_TRANSCRIPTION_SECONDS:
                    raise ValueError("audio input exceeds the maximum duration")
                return " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
            finally:
                source_path.unlink(missing_ok=True)


class LocalVoicesRequestHandler(BaseHTTPRequestHandler):
    service: LocalVoicesService
    transcriber: LocalTranscriptionService
    auth_token: str

    def _authorized(self) -> bool:
        if not self.auth_token:
            return True
        provided = self.headers.get("Authorization", "")
        return hmac.compare_digest(provided, "Bearer " + self.auth_token)

    def _write_headers(self, status: HTTPStatus, content_type: str, content_length: int = 0) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _write_json(self, status: HTTPStatus, data: dict[str, object]) -> None:
        body = json.dumps(data).encode("utf-8")
        self._write_headers(status, "application/json", len(body))
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._write_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        if self.path.rstrip("/") != "/health":
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        tts = self.service.health()
        stt = self.transcriber.health()
        self._write_json(HTTPStatus.OK, {
            **tts,
            "tts": tts,
            "stt": stt,
            "version": 2,
        })

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._write_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        path = self.path.rstrip("/")
        if path == "/v1/speech":
            self._synthesize()
        elif path == "/v1/audio/transcriptions":
            self._transcribe()
        else:
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _synthesize(self) -> None:
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

    def _transcribe(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_AUDIO_BYTES:
                raise ValueError("audio request body has an invalid size")
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in ALLOWED_AUDIO_TYPES:
                raise ValueError("unsupported audio content type")
            suffix = {
                "audio/webm": ".webm",
                "video/webm": ".webm",
                "audio/ogg": ".ogg",
                "audio/wav": ".wav",
                "audio/x-wav": ".wav",
                "audio/mpeg": ".mp3",
                "audio/mp4": ".m4a",
            }[content_type]
            text = self.transcriber.transcribe(self.rfile.read(content_length), suffix)
        except ValueError as error:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except RuntimeError as error:
            self._write_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            return
        except Exception:
            self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "audio transcription failed"})
            return
        self._write_json(HTTPStatus.OK, {"text": text})

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(
    host: str,
    port: int,
    service: LocalVoicesService | None = None,
    transcriber: LocalTranscriptionService | None = None,
    auth_token: str = "",
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Local speech bridge must bind to a loopback address")
    if not auth_token:
        raise ValueError("Local speech bridge requires an authentication token")
    handler = type("ConfiguredLocalVoicesRequestHandler", (LocalVoicesRequestHandler,), {})
    handler.service = service or LocalVoicesService()
    handler.transcriber = transcriber or LocalTranscriptionService()
    handler.auth_token = auth_token
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Eva's local voices bridge.")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback address to bind (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8090, help="Loopback port to bind (default: 8090).")
    parser.add_argument("--reference", type=Path, help="Reference WAV file for this bridge process.")
    parser.add_argument("--token", default=os.getenv("EVA_LOCAL_SPEECH_TOKEN", ""), help="Per-process bearer token supplied by Eva Standalone.")
    args = parser.parse_args()
    if args.reference:
        os.environ["LOCAL_VOICES_REFERENCE"] = str(args.reference.expanduser())
    server = create_server(args.host, args.port, auth_token=args.token)
    print(f"Eva Local Voices bridge listening on http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()