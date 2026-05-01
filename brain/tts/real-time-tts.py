import argparse
import queue
import sys
import time
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import sounddevice as sd
import webrtcvad
from faster_whisper import WhisperModel


SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_MS / 1000)
BYTES_PER_FRAME = FRAME_SAMPLES * 2  # int16 mono audio


class RealtimeSTT:
    def __init__(
        self,
        model_size: str,
        device: str,
        compute_type: str,
        language: str | None,
        vad_level: int,
        end_silence_ms: int,
        partial_interval: float,
        input_device: int | None,
        input_channels: int | None,
        min_rms: float,
        min_speech_ms: int,
        debug_mic: bool,
    ):
        self.audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=200)

        self.model_size = model_size
        self.runtime_device = device
        self.runtime_compute_type = compute_type
        self.model_lock = threading.Lock()
        self.model = self._load_model(device=device, compute_type=compute_type)

        self.language = language
        self.vad = webrtcvad.Vad(vad_level)
        self.end_silence_ms = end_silence_ms
        self.partial_interval = partial_interval
        self.input_device = input_device
        self.stream_channels = self.resolve_input_channels(input_device, input_channels)
        self.min_rms = min_rms
        self.min_speech_ms = min_speech_ms
        self.debug_mic = debug_mic
        self._last_debug_at = 0.0

        self.executor = ThreadPoolExecutor(max_workers=1)
        self.partial_future = None
        self.last_partial_text = ""

    def _load_model(self, device: str, compute_type: str) -> WhisperModel:
        return WhisperModel(
            self.model_size,
            device=device,
            compute_type=compute_type,
        )

    @staticmethod
    def _is_cuda_library_error(exc: BaseException) -> bool:
        message = str(exc).lower()
        cuda_markers = (
            "cublas",
            "cudnn",
            "cuda",
            "could not load library",
            "library",
            "dll",
        )
        return any(marker in message for marker in cuda_markers)

    def _fallback_to_cpu(self, exc: BaseException) -> bool:
        if self.runtime_device == "cpu":
            return False

        if not self._is_cuda_library_error(exc):
            return False

        print(
            "\nCUDA transcription failed because a required NVIDIA DLL is missing. "
            "Falling back to CPU int8 for this run. "
            f"Original error: {exc}",
            file=sys.stderr,
        )
        self.runtime_device = "cpu"
        self.runtime_compute_type = "int8"
        self.model = self._load_model(device="cpu", compute_type="int8")
        return True

    @staticmethod
    def resolve_input_channels(input_device: int | None, requested_channels: int | None) -> int:
        """Pick a PortAudio-safe input channel count.

        Some Windows input devices reject mono streams even when the STT pipeline
        needs mono audio. In that case we open stereo, then downmix to mono in
        audio_callback before feeding WebRTC VAD and Whisper.
        """
        if requested_channels is not None:
            if requested_channels < 1:
                raise ValueError("--input-channels must be 1 or higher")
            return requested_channels

        try:
            device_info = sd.query_devices(input_device, "input")
            max_channels = int(device_info.get("max_input_channels", 0))
        except Exception:
            max_channels = 1

        if max_channels >= 2:
            return 2
        return 1

    def audio_callback(self, indata, frames, time_info, status):
        if status:
            print(f"\nAudio warning: {status}", file=sys.stderr)

        try:
            audio_i16 = np.frombuffer(indata, dtype=np.int16)

            if self.stream_channels > 1:
                usable_samples = (len(audio_i16) // self.stream_channels) * self.stream_channels
                audio_i16 = audio_i16[:usable_samples].reshape(-1, self.stream_channels)
                audio_i16 = audio_i16.astype(np.int32).mean(axis=1).astype(np.int16)

            self.audio_queue.put_nowait(audio_i16.tobytes())
        except queue.Full:
            # Drop audio instead of increasing latency.
            pass

    def transcribe_bytes(self, audio_bytes: bytes) -> str:
        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        if len(audio) < SAMPLE_RATE * 0.25:
            return ""

        with self.model_lock:
            try:
                return self._transcribe_audio(audio)
            except RuntimeError as exc:
                if self._fallback_to_cpu(exc):
                    return self._transcribe_audio(audio)
                raise

    def _transcribe_audio(self, audio: np.ndarray) -> str:
        segments, _ = self.model.transcribe(
            audio,
            language=self.language,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
        )

        return " ".join(segment.text.strip() for segment in segments).strip()

    @staticmethod
    def frame_rms(frame: bytes) -> float:
        audio_i16 = np.frombuffer(frame, dtype=np.int16)
        if len(audio_i16) == 0:
            return 0.0
        return float(np.sqrt(np.mean(audio_i16.astype(np.float32) ** 2)))

    def print_partial_if_ready(self):
        if self.partial_future is None:
            return

        if not self.partial_future.done():
            return

        try:
            text = self.partial_future.result().strip()
        except Exception as exc:
            print(f"\nPartial transcription error: {exc}", file=sys.stderr)
            text = ""

        self.partial_future = None

        if text and text != self.last_partial_text:
            self.last_partial_text = text
            print(f"\rPARTIAL: {text}   ", end="", flush=True)

    def run(self):
        print("Loading microphone stream...")
        if self.input_device is None:
            print("Input device: system default microphone")
        else:
            print(f"Input device index: {self.input_device}")
        print(f"Input stream channels: {self.stream_channels} -> downmixed to mono")
        print(f"Transcription backend: {self.runtime_device} / {self.runtime_compute_type}")
        print("Speak into your microphone. Press Ctrl+C to stop.\n")

        pre_roll = deque(maxlen=10)
        speech_frames: list[bytes] = []

        in_speech = False
        silence_frames = 0
        detected_voice_frames = 0
        last_partial_at = 0.0

        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_SAMPLES,
            dtype="int16",
            channels=self.stream_channels,
            device=self.input_device,
            callback=self.audio_callback,
        ):
            while True:
                frame = self.audio_queue.get()

                if len(frame) != BYTES_PER_FRAME:
                    continue

                raw_vad_voice = self.vad.is_speech(frame, SAMPLE_RATE)
                rms = self.frame_rms(frame)
                is_voice = raw_vad_voice and rms >= self.min_rms

                if self.debug_mic:
                    now_debug = time.monotonic()
                    if now_debug - self._last_debug_at >= 0.25:
                        gate_state = "PASS" if rms >= self.min_rms else "gate"
                        print(
                            f"\rMIC rms={rms:7.1f} vad={'VOICE' if raw_vad_voice else 'silence'} noise={gate_state}     ",
                            end="",
                            flush=True,
                        )
                        self._last_debug_at = now_debug

                if not in_speech:
                    pre_roll.append(frame)

                    if is_voice:
                        in_speech = True
                        speech_frames = list(pre_roll)
                        pre_roll.clear()
                        silence_frames = 0
                        detected_voice_frames = 1
                        last_partial_at = time.monotonic()
                        self.last_partial_text = ""

                    continue

                speech_frames.append(frame)

                if is_voice:
                    silence_frames = 0
                    detected_voice_frames += 1
                else:
                    silence_frames += 1

                now = time.monotonic()
                speech_duration = len(speech_frames) * FRAME_MS / 1000

                if (
                    self.partial_interval > 0
                    and speech_duration >= 0.8
                    and now - last_partial_at >= self.partial_interval
                    and self.partial_future is None
                ):
                    self.partial_future = self.executor.submit(
                        self.transcribe_bytes,
                        b"".join(speech_frames),
                    )
                    last_partial_at = now

                self.print_partial_if_ready()

                silence_duration_ms = silence_frames * FRAME_MS

                if silence_duration_ms >= self.end_silence_ms:
                    if silence_frames > 0:
                        final_frames = speech_frames[:-silence_frames]
                    else:
                        final_frames = speech_frames

                    final_audio = b"".join(final_frames)
                    detected_speech_ms = detected_voice_frames * FRAME_MS

                    print("\r" + " " * 120 + "\r", end="", flush=True)

                    if detected_speech_ms < self.min_speech_ms:
                        final_text = ""
                        if self.debug_mic:
                            print(
                                f"Ignored short/noisy segment ({detected_speech_ms} ms voice)",
                                flush=True,
                            )
                    else:
                        try:
                            final_text = self.transcribe_bytes(final_audio)
                        except Exception as exc:
                            print(f"Final transcription error: {exc}", file=sys.stderr)
                            final_text = ""

                    if final_text:
                        print(f"FINAL: {final_text}")

                    in_speech = False
                    silence_frames = 0
                    detected_voice_frames = 0
                    speech_frames = []
                    self.partial_future = None
                    self.last_partial_text = ""


def list_input_devices() -> None:
    devices = sd.query_devices()
    print("Available input devices:")
    for index, device in enumerate(devices):
        if device.get("max_input_channels", 0) > 0:
            name = device.get("name", "Unknown")
            channels = device.get("max_input_channels", 0)
            default_sr = device.get("default_samplerate", 0)
            print(f"  {index}: {name} | inputs={channels} | default_sr={default_sr}")


def main():
    parser = argparse.ArgumentParser(description="Low-latency local speech-to-text.")
    parser.add_argument("--model", default="tiny.en", help="Example: tiny.en, base.en, small.en, tiny, base, small")
    parser.add_argument("--device", default="auto", help="auto, cpu, or cuda")
    parser.add_argument("--compute-type", default="int8", help="int8 for CPU, float16 for CUDA")
    parser.add_argument("--language", default="en", help="Use en for English, or leave empty for auto-detect")
    parser.add_argument("--vad-level", type=int, default=2, choices=[0, 1, 2, 3], help="3 is most aggressive")
    parser.add_argument("--end-silence-ms", type=int, default=450, help="Lower = faster final result, higher = fewer cutoffs")
    parser.add_argument("--partial-interval", type=float, default=0.8, help="Seconds between partial updates")
    parser.add_argument("--input-device", type=int, default=None, help="Microphone device index from --list-devices")
    parser.add_argument("--input-channels", type=int, default=None, help="Force microphone channels. Try 2 if channel=1 fails on Windows.")
    parser.add_argument("--min-rms", type=float, default=40.0, help="Ignore VAD speech frames quieter than this RMS level")
    parser.add_argument("--min-speech-ms", type=int, default=250, help="Ignore final segments with less detected voice than this")
    parser.add_argument("--list-devices", action="store_true", help="List available microphone input devices and exit")
    parser.add_argument("--debug-mic", action="store_true", help="Show microphone audio level and VAD state")

    args = parser.parse_args()

    if args.list_devices:
        list_input_devices()
        return

    language = args.language.strip() or None

    app = RealtimeSTT(
        model_size=args.model,
        device=args.device,
        compute_type=args.compute_type,
        language=language,
        vad_level=args.vad_level,
        end_silence_ms=args.end_silence_ms,
        partial_interval=args.partial_interval,
        input_device=args.input_device,
        input_channels=args.input_channels,
        min_rms=args.min_rms,
        min_speech_ms=args.min_speech_ms,
        debug_mic=args.debug_mic,
    )

    try:
        app.run()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
