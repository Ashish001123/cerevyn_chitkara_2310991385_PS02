"""Speech-to-text and text-to-speech engines.

Production: Azure AI Speech over its REST APIs (no SDK needed).
Local / offline: faster-whisper for STT, macOS `say` or pyttsx3 for TTS.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import threading
import uuid
import queue
import tempfile
import wave
from concurrent.futures import Future
from pathlib import Path
from typing import Protocol
from xml.sax.saxutils import escape

import httpx

from .config import Settings


class STTEngine(Protocol):
    name: str

    def transcribe(self, audio_path: Path) -> str: ...


class TTSEngine(Protocol):
    name: str

    def synthesize(self, text: str, out_path: Path) -> Path: ...


def speakable(text: str) -> str:
    """Make IDs and abbreviations easy to hear: BK24017 -> 'B K, 2 4 0 1 7'."""
    text = re.sub(r"\bBK(\d{5})\b", lambda m: "B K, " + " ".join(m[1]), text)
    return text.replace(" IST", " I S T")


def to_pcm16_wav(audio_path: Path) -> bytes:
    """Decode any audio container (wav, webm, ogg, mp3) to 16 kHz mono 16-bit WAV bytes."""
    from faster_whisper.audio import decode_audio  # PyAV-backed decoder, no system ffmpeg needed

    samples = decode_audio(str(audio_path), sampling_rate=16000)
    pcm = (samples.clip(-1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return buf.getvalue()

MIN_SPEECH_RMS = float(os.getenv("STT_MIN_SPEECH_RMS", "0.01"))    
RELATIVE_SPEECH_FLOOR = float(os.getenv("STT_RELATIVE_FLOOR", "0.3"))  
NO_SPEECH_PROB, MIN_AVG_LOGPROB, MAX_COMPRESSION_RATIO = 0.6, -1.0, 2.4
NOISE_HALLUCINATIONS = {"", "you", "thank you", "thanks", "thank you very much", "thanks for watching",
                        "thank you for watching", "please subscribe", "bye", "music", "applause", "so"}


def _active_level(chunk) -> float:
    """RMS of the louder half of 20 ms frames, so pauses inside a segment don't dilute it."""
    import numpy as np

    frames = len(chunk) // 320
    if frames == 0:
        return float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
    energy = np.sort(np.mean(chunk[: frames * 320].reshape(frames, 320) ** 2, axis=1))
    return float(np.sqrt(np.mean(energy[frames // 2:])))


def foreground_segments(audio, segments: list[dict]) -> list[dict]:
    """Keep the caller's speech segments; drop distant or much quieter background voices."""
    levels = [_active_level(audio[s["start"]:s["end"]]) for s in segments]
    if not levels:
        return []
    loudest = max(levels)
    return [s for s, level in zip(segments, levels)
            if level >= MIN_SPEECH_RMS and level >= RELATIVE_SPEECH_FLOOR * loudest]


def clean_transcript(segments) -> str:
    """Join Whisper segments, dropping non-speech, looping and known hallucinated output."""
    kept = []
    for s in segments:
        if s.no_speech_prob > NO_SPEECH_PROB and s.avg_logprob < MIN_AVG_LOGPROB:
            continue
        if s.compression_ratio > MAX_COMPRESSION_RATIO:
            continue
        kept.append(s.text.strip())
    text = " ".join(k for k in kept if k).strip()
    return "" if re.sub(r"[^a-z ]", "", text.lower()).strip() in NOISE_HALLUCINATIONS else text



def is_prompt_echo(text: str, prompt: str) -> bool:
    """Whisper sometimes repeats its vocabulary prompt instead of transcribing noisy audio."""
    words = re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()
    prompt_words = " ".join(re.sub(r"[^a-z0-9 ]", " ", prompt.lower()).split())
    return len(words) >= 3 and f" {' '.join(words)} " in f" {prompt_words} "

class WhisperSTT:
    name = "faster-whisper"

    def __init__(self, model_size: str, vocabulary: str = ""):
        self.model_size = model_size
        self.vocabulary = vocabulary
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8",
                                           cpu_threads=os.cpu_count() or 4)
        return self._model

    def warm_up(self) -> None:
        """Load the model and run one tiny inference so the first real turn is fast."""
        import numpy as np
        list(self._load().transcribe(np.zeros(16000, dtype=np.float32), language="en", beam_size=1)[0])

    def transcribe(self, audio_path: Path) -> str:
        import numpy as np
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        try:
            audio = decode_audio(str(audio_path), sampling_rate=16000)
        except Exception:
            return ""
        if len(audio) < 16000 * 0.3:
            return ""
        vad = VadOptions(threshold=0.5, min_speech_duration_ms=150, min_silence_duration_ms=1000, speech_pad_ms=400)
        speech = foreground_segments(audio, get_speech_timestamps(audio, vad))
        if not speech:
            return ""
        gap = np.zeros(1600, dtype=np.float32)
        voiced = np.concatenate([part for s in speech for part in (audio[s["start"]:s["end"]], gap)])
        segments, _ = self._load().transcribe(
            voiced, language="en", beam_size=1, vad_filter=False, without_timestamps=True,
            condition_on_previous_text=False, initial_prompt=self.vocabulary or None,
        )
        text = clean_transcript(list(segments))
        return "" if self.vocabulary and is_prompt_echo(text, self.vocabulary) else text


class AzureSTT:
    name = "azure-speech"

    def __init__(self, key: str, region: str, language: str):
        self.url = (f"https://{region}.stt.speech.microsoft.com/speech/recognition/conversation/"
                    f"cognitiveservices/v1?language={language}&format=simple")
        self.key = key

    def transcribe(self, audio_path: Path) -> str:
        resp = httpx.post(
            self.url, content=to_pcm16_wav(audio_path), timeout=30,
            headers={"Ocp-Apim-Subscription-Key": self.key,
                     "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000",
                     "Accept": "application/json"},
        )
        resp.raise_for_status()
        body = resp.json()
        return body.get("DisplayText", "") if body.get("RecognitionStatus") == "Success" else ""


class MacSayTTS:
    name = "macos-say"

    def __init__(self, voice: str | None):
        voices = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
        self.voice = voice if voice and re.search(rf"^{re.escape(voice)}\s", voices, re.M) else None

    def synthesize(self, text: str, out_path: Path) -> Path:
        cmd = ["say", "-o", str(out_path), "--data-format=LEI16@16000"]
        if self.voice:
            cmd += ["-v", self.voice]
        subprocess.run(cmd + [speakable(text)], check=True, capture_output=True)
        return out_path


class NativeMacTTS:
    """macOS NSSpeechSynthesizer kept loaded in-process.

    Spawning `say` costs ~0.6 s per reply; a resident synthesizer renders a
    10-second reply in ~0.2 s. AppKit objects stay on one dedicated thread.
    """
    name = "macos-native"

    def __init__(self, voice: str | None):
        from AppKit import NSSpeechSynthesizer  
        self.voice = voice
        self._jobs: queue.Queue = queue.Queue()
        ready = threading.Event()
        threading.Thread(target=self._worker, args=(ready,), daemon=True).start()
        ready.wait(10)

    def _worker(self, ready: threading.Event) -> None:
        from AppKit import NSSpeechSynthesizer
        from Foundation import NSDate, NSRunLoop, NSURL

        voices = [v for v in NSSpeechSynthesizer.availableVoices() if self.voice and v.endswith("." + self.voice)]
        synth = NSSpeechSynthesizer.alloc().initWithVoice_(voices[0] if voices else None)

        def render(text: str, aiff: Path) -> None:
            synth.startSpeakingString_toURL_(text, NSURL.fileURLWithPath_(str(aiff)))
            while synth.isSpeaking():
                NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.005))

        warm = Path(tempfile.gettempdir()) / f"tts-warmup-{uuid.uuid4().hex[:6]}.aiff"
        render("Hello.", warm)
        warm.unlink(missing_ok=True)
        ready.set()
        while True:
            text, out_path, done = self._jobs.get()
            try:
                aiff = out_path.with_suffix(".aiff")
                render(text, aiff)
                _write_wav16k(aiff, out_path)
                aiff.unlink(missing_ok=True)
                done.set_result(out_path)
            except Exception as exc:
                done.set_exception(exc)

    def synthesize(self, text: str, out_path: Path) -> Path:
        done: Future = Future()
        self._jobs.put((speakable(text), out_path, done))
        return done.result(timeout=30)


def _write_wav16k(src: Path, dst: Path) -> None:
    from faster_whisper.audio import decode_audio

    samples = decode_audio(str(src), sampling_rate=16000)
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((samples.clip(-1, 1) * 32767).astype("<i2").tobytes())


class Pyttsx3TTS:
    name = "pyttsx3"

    def __init__(self):
        import pyttsx3      
        self._pyttsx3 = pyttsx3
        self._lock = threading.Lock()

    def synthesize(self, text: str, out_path: Path) -> Path:
        with self._lock:
            engine = self._pyttsx3.init()
            engine.save_to_file(speakable(text), str(out_path))
            engine.runAndWait()
            engine.stop()
        return out_path


class AzureTTS:
    name = "azure-speech"

    def __init__(self, key: str, region: str, voice: str):
        self.url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
        self.key = key
        self.voice = voice

    def synthesize(self, text: str, out_path: Path) -> Path:
        lang = "-".join(self.voice.split("-")[:2])
        ssml = (f"<speak version='1.0' xml:lang='{lang}'><voice name='{self.voice}'>"
                f"{escape(speakable(text))}</voice></speak>")
        resp = httpx.post(
            self.url, content=ssml.encode(), timeout=30,
            headers={"Ocp-Apim-Subscription-Key": self.key,
                     "Content-Type": "application/ssml+xml",
                     "X-Microsoft-OutputFormat": "riff-16khz-16bit-mono-pcm",
                     "User-Agent": "voice-appointment-agent"},
        )
        resp.raise_for_status()
        out_path.write_bytes(resp.content)
        return out_path

def stt_vocabulary(services: list[dict]) -> str:
    names = ", ".join(f"{s['service']} at {s['name']}" for s in services)
    return (f"Appointment booking call. Services: {names}. "
            "Dates like October 20th at 10 AM. Booking ID BK followed by five digits.")


def build_speech(settings: Settings, services: list[dict]) -> tuple[STTEngine, TTSEngine]:
    use_azure = settings.speech_provider == "azure" or (
        settings.speech_provider == "auto" and settings.azure_speech_key and settings.azure_speech_region)
    if use_azure:
        if not (settings.azure_speech_key and settings.azure_speech_region):
            raise RuntimeError("SPEECH_PROVIDER=azure needs AZURE_SPEECH_KEY and AZURE_SPEECH_REGION")
        return (AzureSTT(settings.azure_speech_key, settings.azure_speech_region, settings.azure_stt_language),
                AzureTTS(settings.azure_speech_key, settings.azure_speech_region, settings.azure_tts_voice))
    stt = WhisperSTT(settings.whisper_model, stt_vocabulary(services))
    return stt, _local_tts(settings.local_tts_voice)


def _local_tts(voice: str | None) -> TTSEngine:
    if shutil.which("say"):
        try:
            return NativeMacTTS(voice)
        except ImportError:
            return MacSayTTS(voice) 
    return Pyttsx3TTS()


def new_audio_path(directory: Path, prefix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{prefix}-{uuid.uuid4().hex[:10]}.wav"
