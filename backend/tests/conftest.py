from __future__ import annotations

import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings, make_clock  # noqa: E402
from app.db import seed_database  # noqa: E402
from app.dialogue import DialogueManager  # noqa: E402
from app.repository import Repository  # noqa: E402

FIXED_NOW = "2026-10-11T09:00:00+05:30"
SEED = ROOT / "data" / "seed.json"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    seed_database(path, SEED, reset=True)
    return path


@pytest.fixture
def clock():
    return make_clock(FIXED_NOW)


@pytest.fixture
def repo(db_path, clock):
    return Repository(db_path, clock)


@pytest.fixture
def dm(repo, clock):
    return DialogueManager(repo, clock)


class Conversation:
    """Drive one session in text mode and keep the last reply/state."""

    def __init__(self, dm: DialogueManager):
        self.dm = dm
        self.session_id, self.reply, self.state, _ = dm.start_session()

    def say(self, text: str) -> str:
        self.reply, self.state, _ = self.dm.handle(self.session_id, text)
        return self.reply


@pytest.fixture
def convo(dm):
    return lambda: Conversation(dm)


class ScriptedSTT:
    """Returns queued transcripts in order, so API tests stay fast and deterministic."""
    name = "scripted-stt"

    def __init__(self):
        self.queue: list[str] = []

    def transcribe(self, audio_path: Path) -> str:
        return self.queue.pop(0)


class SilentTTS:
    name = "silent-tts"

    def synthesize(self, text: str, out_path: Path) -> Path:
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 1600)
        return out_path


@pytest.fixture
def api(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = Settings(db_path=tmp_path / "api.db", seed_path=SEED, audio_out_dir=tmp_path / "tts",
                        app_now=FIXED_NOW)
    stt = ScriptedSTT()
    app = create_app(settings, stt=stt, tts=SilentTTS())
    with TestClient(app) as client:
        client.stt = stt
        yield client
