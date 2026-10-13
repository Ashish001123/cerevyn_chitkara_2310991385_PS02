"""Live call pipeline: streamed mic audio -> turn detection -> STT -> dialogue -> streamed TTS.

`CallSession.feed` is cheap (Silero VAD runs in ~1 ms per 32 ms frame) and is
called for every audio chunk. It returns events; `_speculate` and `_turn` carry
the caller's audio so the transport layer can run STT off the event loop.
`respond` is a generator that yields the reply sentence by sentence, so the
first sentence plays while the rest is still being synthesized.
"""

from __future__ import annotations

import base64
import re
import time
import uuid
import wave
from collections import deque
from pathlib import Path

import numpy as np

from .dialogue import DialogueManager
from .speech import STTEngine, TTSEngine
from .turns import FRAME_MS, TurnConfig, TurnDetector

SR = 16000
FRAME = SR * FRAME_MS // 1000          # 512 samples
PREROLL_FRAMES = 10                    # ~320 ms kept from before the turn started (word onsets)
MIN_TURN_SAMPLES = SR                  # shorter "turns" with no words (coughs, clicks) are ignored
REPEAT_PROMPT = "Sorry, could you say that again?"


class StreamingVAD:
    """Silero VAD (bundled with faster-whisper) run one frame at a time with carried state."""

    def __init__(self):
        from faster_whisper.vad import get_vad_model

        self._session = get_vad_model().session
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(64, dtype=np.float32)

    def prob(self, frame: np.ndarray) -> float:
        x = np.concatenate([self._context, frame.astype(np.float32)])[None, :]
        out, self._h, self._c = self._session.run(None, {"input": x, "h": self._h, "c": self._c})
        self._context = frame[-64:].astype(np.float32)
        return float(np.asarray(out).reshape(-1)[0])


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<!\bDr\.)(?<!\bMr\.)(?<!\bMs\.)(?<=[.?!])\s+", text.strip())
    return [p for p in parts if p]


class CallSession:
    def __init__(self, dialogue: DialogueManager, stt: STTEngine, tts: TTSEngine, session_id: str,
                 audio_dir: Path, vad: StreamingVAD | None = None, config: TurnConfig | None = None):
        self.dialogue, self.stt, self.tts, self.session_id = dialogue, stt, tts, session_id
        self.audio_dir = Path(audio_dir)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.vad = vad or StreamingVAD()
        self.turns = TurnDetector(config)
        self._pending = b""
        self._preroll: deque[np.ndarray] = deque(maxlen=PREROLL_FRAMES)
        self._utterance: list[np.ndarray] = []
        self._cancel = False
        self._last_audio_path: str | None = None

    # ------------------------------------------------------------------- input
    @property
    def agent_speaking(self) -> bool:
        return self.turns.agent_speaking

    @agent_speaking.setter
    def agent_speaking(self, value: bool) -> None:
        self.turns.agent_speaking = value

    def feed(self, pcm16: bytes) -> list[dict]:
        """Consume raw 16 kHz mono PCM16 audio; return turn events."""
        data = self._pending + pcm16
        usable = len(data) - len(data) % (FRAME * 2)
        self._pending = data[usable:]
        events: list[dict] = []
        for offset in range(0, usable, FRAME * 2):
            frame = np.frombuffer(data[offset:offset + FRAME * 2], dtype="<i2").astype(np.float32) / 32768
            level = float(np.sqrt(np.mean(frame ** 2)))
            event = self.turns.push(self.vad.prob(frame), level)
            if self.turns.in_speech or event == "end":
                if event == "start":
                    self._utterance = list(self._preroll)
                self._utterance.append(frame)
            else:
                self._preroll.append(frame)
            if event == "start":
                events.append({"type": "user_speech_start"})
            elif event == "speculate":
                events.append({"type": "_speculate", "audio": np.concatenate(self._utterance)})
            elif event == "resume":
                events.append({"type": "_resume"})
            elif event == "end":
                events.append({"type": "_turn", "audio": np.concatenate(self._utterance)})
                self._utterance = []
                self._preroll.clear()
        return events

    # ------------------------------------------------------------------ output
    def transcribe(self, audio: np.ndarray) -> str:
        path = self.audio_dir / f"call-{uuid.uuid4().hex[:10]}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        self._last_audio_path = str(path)
        return self.stt.transcribe(path)

    def cancel_speech(self) -> None:
        """Barge-in: stop synthesizing/sending the rest of the current reply."""
        self._cancel = True

    def respond(self, audio: np.ndarray, transcript: str | None = None):
        """Yield transcript, agent_text, agent_audio (one per sentence) and timings events."""
        self._cancel = False
        t0 = time.perf_counter()
        text = transcript if transcript is not None else self.transcribe(audio)
        stt_ms = round((time.perf_counter() - t0) * 1000)
        if not text:
            if len(audio) < MIN_TURN_SAMPLES:
                yield {"type": "ignored"}
                return
            reply, state = REPEAT_PROMPT, self.dialogue.repo.load_state(self.session_id)
        else:
            yield {"type": "transcript", "text": text}
            reply, state, _ = self.dialogue.handle(self.session_id, text, user_audio=self._last_audio_path)
        yield {"type": "agent_text", "text": reply, "state": state}
        yield from self.speak(reply, t0, stt_ms)

    def speak(self, text: str, t0: float | None = None, stt_ms: int = 0):
        """Synthesize and yield the reply one sentence at a time, then a timings event."""
        t0 = t0 or time.perf_counter()
        first_audio_ms = None
        sentences = split_sentences(text)
        for i, sentence in enumerate(sentences):
            if self._cancel:
                break
            path = Path(self.tts.synthesize(sentence, self.audio_dir / f"agent-{uuid.uuid4().hex[:10]}.wav"))
            if first_audio_ms is None:
                first_audio_ms = round((time.perf_counter() - t0) * 1000)
            yield {"type": "agent_audio", "index": i, "last": i == len(sentences) - 1,
                   "audio_b64": base64.b64encode(path.read_bytes()).decode()}
        yield {"type": "timings", "stt_ms": stt_ms, "first_audio_ms": first_audio_ms,
               "total_ms": round((time.perf_counter() - t0) * 1000)}
