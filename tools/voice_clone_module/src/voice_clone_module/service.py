from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any

import torch


MAX_SYNTHESIS_CHARS = 600


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
        self._model = model
        self._prepared_reference: Path | None = None

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
        """Load and cache Chatterbox on first use."""
        if self._model is None:
            from chatterbox.tts import ChatterboxTTS

            self._model = ChatterboxTTS.from_pretrained(device=self.device)
        return self._model

    def _prepare_reference(self, reference: Path) -> None:
        if self._prepared_reference == reference:
            return
        self.model.prepare_conditionals(str(reference), exaggeration=self.exaggeration)
        self._prepared_reference = reference

    def synthesize(
        self,
        text: str,
        *,
        reference_audio: str | Path | None = None,
        exaggeration: float | None = None,
        cfg_weight: float | None = None,
    ) -> torch.Tensor:
        """Generate a watermarked waveform using the configured authorized voice."""
        if not text.strip():
            raise ValueError("text must not be empty")

        reference = Path(reference_audio) if reference_audio else self.reference_audio
        if reference is None:
            raise ValueError("a reference audio path is required")
        if not reference.is_file():
            raise FileNotFoundError(f"reference audio does not exist: {reference}")

        exaggeration_value = self.exaggeration if exaggeration is None else exaggeration
        cfg_weight_value = self.cfg_weight if cfg_weight is None else cfg_weight
        if exaggeration_value != self.exaggeration:
            self._prepared_reference = None
            self.exaggeration = exaggeration_value
        self._prepare_reference(reference)
        waveforms = [
            self.model.generate(
                chunk,
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
        )
        soundfile.write(
            str(output),
            waveform.detach().cpu().squeeze().numpy(),
            self.model.sr,
        )
        return output
