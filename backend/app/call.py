"""Live call pipeline: streamed mic audio -> turn detection -> STT.

`CallSession.feed` runs Silero VAD in ~1 ms per 32 ms frame and is
called for every audio chunk. It emits speculative and turn events.
"""

from __future__ import annotations

import uuid
import wave
from collections import deque
from pathlib import Path

import numpy as np

from .dialogue import DialogueManager
from .speech import STTEngine, TTSEngine
from .turns import FRAME_MS, TurnConfig, TurnDetector

SR = 16000
FRAME = SR * FRAME_MS // 1000        
PREROLL_FRAMES = 10                   
MIN_TURN_SAMPLES = SR                 
REPEAT_PROMPT = "Sorry, could you say that again?"


class StreamingVAD:
    """Silero VAD run one frame at a time with carried state."""

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

    def transcribe(self, audio: np.ndarray) -> str:
        path = self.audio_dir / f"call-{uuid.uuid4().hex[:10]}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        self._last_audio_path = str(path)
        return self.stt.transcribe(path)
