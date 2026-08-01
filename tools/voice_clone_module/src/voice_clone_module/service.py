from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any
import warnings

import torch


# Diffusers emits this upstream migration notice while Chatterbox loads. It is
# not actionable for Eva and does not affect generated audio.
warnings.filterwarnings(
    "ignore",
    message=r"`LoRACompatibleLinear` is deprecated.*",
    category=FutureWarning,
    module=r"diffusers\.models\.lora",
)


MAX_SYNTHESIS_CHARS = 600
SUPPORTED_LANGUAGES = {"en", "ko"}


def detect_language(text: str) -> str:
    """Choose Korean when Hangul is the dominant letter script; otherwise English."""
    value = str(text or "")
    korean = sum("\uac00" <= character <= "\ud7a3" for character in value)
    latin = sum(character.isascii() and character.isalpha() for character in value)
    return "ko" if korean > latin else "en"


def resolve_language(language: str | None, text: str) -> str:
    """Validate a requested language or resolve Eva's deterministic auto mode."""
    value = str(language or "auto").strip().lower()
    if value == "auto":
        return detect_language(text)
    if value not in SUPPORTED_LANGUAGES:
        raise ValueError("unsupported language; supported values are auto, en, and ko")
    return value


def split_synthesis_text(text: str, max_chars: int = MAX_SYNTHESIS_CHARS) -> list[str]:
    """Split long replies at sentence and word boundaries below the model limit."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")

    sentences = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
    chunks: list[str] = []
    current = ""

    for sentence in sentences:
        words = sentence.split()
        while words:
            word = words.pop(0)
            if len(word) > max_chars:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.extend(word[index : index + max_chars] for index in range(0, len(word), max_chars))
                continue

            candidate = f"{current} {word}".strip()
            if current and len(candidate) > max_chars:
                chunks.append(current)
                current = word
            else:
                current = candidate

        if current and sentence.endswith((".", "!", "?")):
            chunks.append(current)
            current = ""

    if current:
        chunks.append(current)
    return chunks


@dataclass(frozen=True)
class VoiceCloneConfig:
    """Defaults used for one voice across repeated synthesis calls."""

    reference_audio: Path | None = None
    device: str = "auto"
    exaggeration: float = 0.5
    cfg_weight: float = 0.5

    @classmethod
    def from_environment(cls) -> "VoiceCloneConfig":
        reference = os.getenv("VOICE_CLONE_REFERENCE")
        return cls(
            reference_audio=Path(reference) if reference else None,
            device=os.getenv("VOICE_CLONE_DEVICE", "auto"),
            exaggeration=float(os.getenv("VOICE_CLONE_EXAGGERATION", "0.5")),
            cfg_weight=float(os.getenv("VOICE_CLONE_CFG_WEIGHT", "0.5")),
        )


def choose_device(requested: str) -> str:
    """Resolve ``auto`` and fail early for an unavailable requested device."""
    if requested == "auto":
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    if requested == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested, but no CUDA device is available")
    if requested == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("MPS was requested, but no MPS device is available")
    if requested not in {"cpu", "cuda", "mps"}:
        raise ValueError(f"Unsupported device: {requested}")
    return requested


class VoiceCloner:
    """Reusable Chatterbox voice clone service for Eva's local speech bridge."""

    def __init__(
        self,
        reference_audio: str | Path | None = None,
        *,
        device: str = "auto",
        exaggeration: float = 0.5,
        cfg_weight: float = 0.5,
        model: Any | None = None,
    ) -> None:
        self.reference_audio = Path(reference_audio) if reference_audio else None
        self.device = choose_device(device)
        self.exaggeration = exaggeration
        self.cfg_weight = cfg_weight
        self._english_model = model
        self._multilingual_model: Any | None = None
        self._prepared_references: dict[str, tuple[Path, float]] = {}

    @classmethod
    def from_environment(cls) -> "VoiceCloner":
        config = VoiceCloneConfig.from_environment()
        return cls(
            reference_audio=config.reference_audio,
            device=config.device,
            exaggeration=config.exaggeration,
            cfg_weight=config.cfg_weight,
        )

    @property
    def model(self) -> Any:
        """Load and cache the existing English Chatterbox model."""
        if self._english_model is None:
            from chatterbox.tts import ChatterboxTTS

            self._english_model = ChatterboxTTS.from_pretrained(device=self.device)
        return self._english_model

    @property
    def loaded_languages(self) -> list[str]:
        languages = []
        if self._english_model is not None:
            languages.append("en")
        if self._multilingual_model is not None:
            languages.append("ko")
        return languages

    def _model_for(self, language: str) -> Any:
        if language == "en":
            return self.model
        if self._multilingual_model is None:
            from chatterbox.mtl_tts import ChatterboxMultilingualTTS

            self._multilingual_model = ChatterboxMultilingualTTS.from_pretrained(
                device=self.device,
                t3_model=os.getenv("VOICE_CLONE_MULTILINGUAL_MODEL", "v3"),
            )
        return self._multilingual_model

    def _prepare_reference(self, reference: Path, language: str, model: Any, exaggeration: float) -> None:
        if self._prepared_references.get(language) == (reference, exaggeration):
            return
        model.prepare_conditionals(str(reference), exaggeration=exaggeration)
        self._prepared_references[language] = (reference, exaggeration)

    def synthesize(
        self,
        text: str,
        *,
        reference_audio: str | Path | None = None,
        exaggeration: float | None = None,
        cfg_weight: float | None = None,
        language: str | None = "auto",
    ) -> torch.Tensor:
        """Generate a watermarked waveform using the configured authorized voice."""
        if not text.strip():
            raise ValueError("text must not be empty")

        reference = Path(reference_audio) if reference_audio else self.reference_audio
        if reference is None:
            raise ValueError("a reference audio path is required")
        if not reference.is_file():
            raise FileNotFoundError(f"reference audio does not exist: {reference}")

        resolved_language = resolve_language(language, text)
        exaggeration_value = self.exaggeration if exaggeration is None else exaggeration
        cfg_weight_value = self.cfg_weight if cfg_weight is None else cfg_weight
        model = self._model_for(resolved_language)
        self._prepare_reference(reference, resolved_language, model, exaggeration_value)
        if resolved_language == "en":
            waveforms = [
                model.generate(
                    chunk,
                    exaggeration=exaggeration_value,
                    cfg_weight=cfg_weight_value,
                )
                for chunk in split_synthesis_text(text)
            ]
        else:
            waveforms = [
                model.generate(
                    chunk,
                    language_id=resolved_language,
                    exaggeration=exaggeration_value,
                    cfg_weight=cfg_weight_value,
                )
                for chunk in split_synthesis_text(text)
            ]
        return torch.cat(waveforms, dim=-1)

    def save(
        self,
        text: str,
        output_path: str | Path,
        *,
        reference_audio: str | Path | None = None,
        exaggeration: float | None = None,
        cfg_weight: float | None = None,
        language: str | None = "auto",
    ) -> Path:
        """Synthesize speech and save it as a WAV file."""
        import soundfile

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        waveform = self.synthesize(
            text,
            reference_audio=reference_audio,
            exaggeration=exaggeration,
            cfg_weight=cfg_weight,
            language=language,
        )
        soundfile.write(
            str(output),
            waveform.detach().cpu().squeeze().numpy(),
            self._model_for(resolve_language(language, text)).sr,
        )
        return output
