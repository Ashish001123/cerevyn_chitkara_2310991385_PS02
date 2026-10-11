"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(os.getenv("DB_PATH", ROOT / "data" / "appointments.db")))
    seed_path: Path = field(default_factory=lambda: Path(os.getenv("SEED_PATH", ROOT / "data" / "seed.json")))
    audio_out_dir: Path = field(default_factory=lambda: Path(os.getenv("AUDIO_OUT_DIR", ROOT / "data" / "tts_out")))
    # "auto" uses Azure AI Speech when a key is configured, otherwise local engines.
    speech_provider: str = field(default_factory=lambda: os.getenv("SPEECH_PROVIDER", "auto"))
    azure_speech_key: str | None = field(default_factory=lambda: os.getenv("AZURE_SPEECH_KEY"))
    azure_speech_region: str | None = field(default_factory=lambda: os.getenv("AZURE_SPEECH_REGION"))
    azure_stt_language: str = field(default_factory=lambda: os.getenv("AZURE_STT_LANGUAGE", "en-IN"))
    azure_tts_voice: str = field(default_factory=lambda: os.getenv("AZURE_TTS_VOICE", "en-IN-NeerjaNeural"))
    whisper_model: str = field(default_factory=lambda: os.getenv("WHISPER_MODEL", "small.en"))
    local_tts_voice: str = field(default_factory=lambda: os.getenv("LOCAL_TTS_VOICE", "Samantha"))
    # Optional fixed clock (ISO-8601). Lets the seeded October 2026 slots stay
    # bookable when the project is run after those dates.
    app_now: str | None = field(default_factory=lambda: os.getenv("APP_NOW", "2026-10-11T09:00:00+05:30"))


def make_clock(app_now: str | None):
    """Return a zero-arg callable giving the current time in Asia/Kolkata."""
    if app_now:
        fixed = datetime.fromisoformat(app_now).astimezone(TZ)
        return lambda: fixed
    return lambda: datetime.now(TZ)
