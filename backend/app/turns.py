"""End-of-turn detection for live calls.

Fed one 32 ms frame at a time with the Silero speech probability and the frame
level. Emits:
  "start"     the caller started talking (also used for barge-in)
  "speculate" a short pause: STT may start early on what was said so far
  "resume"    the caller kept talking after a speculative pause
  "end"       the caller finished; respond now
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .speech import MIN_SPEECH_RMS

FRAME_MS = 32


@dataclass(frozen=True)
class TurnConfig:
    start_ms: int = 160              
    speculate_ms: int = 250          
    end_silence_ms: int = 500        
    max_turn_ms: int = 15000
    speech_prob: float = 0.5
    min_level: float = MIN_SPEECH_RMS 
    barge_in_ms: int = 320
    barge_in_prob: float = 0.8
    barge_in_level_factor: float = 2.0


def _frames(ms: int) -> int:
    return max(1, math.ceil(ms / FRAME_MS))


class TurnDetector:
    def __init__(self, config: TurnConfig | None = None):
        self.config = config or TurnConfig()
        self.agent_speaking = False
        self.reset()

    def reset(self) -> None:
        self.in_speech = False
        self._voiced_run = 0
        self._silence = 0
        self._turn_frames = 0
        self._speculated = False

    def _voiced(self, prob: float, level: float, barge_in: bool) -> bool:
        c = self.config
        if barge_in:
            return prob >= c.barge_in_prob and level >= c.min_level * c.barge_in_level_factor
        return prob >= c.speech_prob and level >= c.min_level

    def push(self, prob: float, level: float) -> str | None:
        c = self.config
        if not self.in_speech:
            barge_in = self.agent_speaking
            self._voiced_run = self._voiced_run + 1 if self._voiced(prob, level, barge_in) else 0
            if self._voiced_run >= _frames(c.barge_in_ms if barge_in else c.start_ms):
                self.in_speech, self._silence, self._turn_frames, self._speculated = True, 0, self._voiced_run, False
                self._voiced_run = 0
                return "start"
            return None

        self._turn_frames += 1
        if self._turn_frames >= _frames(c.max_turn_ms):
            self.reset()
            return "end"
        if self._voiced(prob, level, barge_in=False):
            resumed = self._speculated
            self._silence, self._speculated = 0, False
            return "resume" if resumed else None
        self._silence += 1
        if self._silence >= _frames(c.end_silence_ms):
            self.reset()
            return "end"
        if self._silence == _frames(c.speculate_ms):
            self._speculated = True
            return "speculate"
        return None
