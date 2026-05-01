from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from typing import Optional, Protocol


DEFAULT_SAMPLE_RATE = 16000
DEFAULT_MODEL_SIZE = "tiny.en"
MAX_AUDIO_BYTES = 20 * 1024 * 1024


@dataclass
class STTResult:
    transcript: str = ""
    ok: bool = True
    reason: str = "ok"
    language: Optional[str] = None
    duration_ms: Optional[int] = None
    backend: str = "local_whisper"
    sample_rate: int = DEFAULT_SAMPLE_RATE
    audio_format: str = "wav"

    def to_json_dict(self) -> dict:
        return {
            "ok": bool(self.ok),
            "reason": str(self.reason or ("ok" if self.ok else "failed")),
            "transcript": str(self.transcript or ""),
            "language": self.language,
            "duration_ms": self.duration_ms,
            "backend": str(self.backend or "local_whisper"),
            "sample_rate": int(self.sample_rate or DEFAULT_SAMPLE_RATE),
            "audio_format": str(self.audio_format or "wav"),
        }


class STTTranscriber(Protocol):
    def transcribe(self, audio_bytes: bytes, *, sample_rate: int, audio_format: str, language: str | None) -> STTResult: ...


class LocalWhisperTranscriber:
    def __init__(self, *, model_size: str = DEFAULT_MODEL_SIZE, device: str = "auto", compute_type: str = "int8") -> None:
        self.model_size = str(model_size or DEFAULT_MODEL_SIZE)
        self.device = str(device or "auto")
        self.compute_type = str(compute_type or "int8")
        self._model = None

    def transcribe(self, audio_bytes: bytes, *, sample_rate: int, audio_format: str, language: str | None) -> STTResult:
        audio, resolved_sample_rate, duration_ms = self._decode_audio(audio_bytes, sample_rate=sample_rate, audio_format=audio_format)
        if len(audio) < max(1, int(resolved_sample_rate * 0.1)):
            return STTResult(ok=False, reason="audio_too_short", sample_rate=resolved_sample_rate, audio_format=audio_format)

        model = self._ensure_model()
        segments, info = model.transcribe(
            audio,
            language=(str(language).strip() or None) if language else None,
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        transcript = " ".join(segment.text.strip() for segment in segments).strip()
        detected_language = str(getattr(info, "language", "") or language or "").strip() or None
        return STTResult(
            ok=bool(transcript),
            reason=("ok" if transcript else "empty_transcript"),
            transcript=transcript,
            language=detected_language,
            duration_ms=duration_ms,
            backend=f"faster_whisper:{self.model_size}",
            sample_rate=resolved_sample_rate,
            audio_format=audio_format,
        )

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type)
        return self._model

    def _decode_audio(self, audio_bytes: bytes, *, sample_rate: int, audio_format: str):
        import numpy as np

        fmt = str(audio_format or "wav").strip().lower()
        if fmt == "wav":
            with wave.open(io.BytesIO(audio_bytes), "rb") as wav:
                channels = int(wav.getnchannels())
                width = int(wav.getsampwidth())
                resolved_rate = int(wav.getframerate())
                frame_count = int(wav.getnframes())
                raw = wav.readframes(frame_count)
            if width != 2:
                raise ValueError("unsupported_wav_sample_width")
            samples = np.frombuffer(raw, dtype=np.int16)
            if channels > 1:
                usable = (len(samples) // channels) * channels
                samples = samples[:usable].reshape(-1, channels).astype(np.int32).mean(axis=1).astype(np.int16)
            audio = samples.astype(np.float32) / 32768.0
            duration_ms = int((len(audio) / max(1, resolved_rate)) * 1000)
            return audio, resolved_rate, duration_ms

        if fmt in {"pcm_s16le", "raw_s16le"}:
            resolved_rate = int(sample_rate or DEFAULT_SAMPLE_RATE)
            samples = np.frombuffer(audio_bytes, dtype=np.int16)
            audio = samples.astype(np.float32) / 32768.0
            duration_ms = int((len(audio) / max(1, resolved_rate)) * 1000)
            return audio, resolved_rate, duration_ms

        raise ValueError("unsupported_audio_format")


class LocalSTTService:
    def __init__(
        self,
        *,
        transcriber: STTTranscriber | None = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        max_audio_bytes: int = MAX_AUDIO_BYTES,
    ) -> None:
        self.transcriber = transcriber or LocalWhisperTranscriber()
        self.sample_rate = int(sample_rate or DEFAULT_SAMPLE_RATE)
        self.max_audio_bytes = int(max_audio_bytes or MAX_AUDIO_BYTES)

    def transcribe_audio(self, audio_bytes: bytes, *, audio_format: str = "wav", language: str | None = "en") -> STTResult:
        data = bytes(audio_bytes or b"")
        fmt = str(audio_format or "wav").strip().lower()
        if not data:
            return STTResult(ok=False, reason="empty_audio", sample_rate=self.sample_rate, audio_format=fmt)
        if len(data) > self.max_audio_bytes:
            return STTResult(ok=False, reason="audio_too_large", sample_rate=self.sample_rate, audio_format=fmt)
        if fmt not in {"wav", "pcm_s16le", "raw_s16le"}:
            return STTResult(ok=False, reason="unsupported_audio_format", sample_rate=self.sample_rate, audio_format=fmt)

        try:
            result = self.transcriber.transcribe(
                data,
                sample_rate=self.sample_rate,
                audio_format=fmt,
                language=(str(language).strip() or None) if language else None,
            )
        except Exception as exc:
            return STTResult(
                ok=False,
                reason=f"transcription_failed:{type(exc).__name__}",
                sample_rate=self.sample_rate,
                audio_format=fmt,
            )
        result.audio_format = fmt
        if not result.sample_rate:
            result.sample_rate = self.sample_rate
        return result
