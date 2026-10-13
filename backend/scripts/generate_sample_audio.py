"""Synthesize the spoken customer requests in samples/requests.json to WAV files.

Uses a different voice per customer so the demo exercises real speech
recognition on varied voices. Uses macOS `say`, or Azure Speech when `say`
is unavailable and AZURE_SPEECH_KEY / AZURE_SPEECH_REGION are set.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ROOT, Settings  # noqa: E402
from app.speech import AzureTTS, MacSayTTS  # noqa: E402

MANIFEST = ROOT / "samples" / "requests.json"
OUT_DIR = ROOT / "samples" / "audio"


def main() -> None:
    settings = Settings()
    manifest = json.loads(MANIFEST.read_text())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if shutil.which("say"):
        make_tts = MacSayTTS
    elif settings.azure_speech_key and settings.azure_speech_region:
        make_tts = lambda _voice: AzureTTS(settings.azure_speech_key, settings.azure_speech_region,  # noqa: E731
                                           "en-IN-PrabhatNeural")
    else:
        sys.exit("Need macOS `say` or AZURE_SPEECH_KEY + AZURE_SPEECH_REGION to synthesize sample audio.")
    for scenario in manifest["scenarios"]:
        for session in scenario["sessions"]:
            for turn in session["turns"]:
                out = OUT_DIR / f"{turn['id']}.wav"
                make_tts(turn["voice"]).synthesize(turn["text"], out)
                print(f"{out.relative_to(ROOT)}  <- {turn['text']}")


if __name__ == "__main__":
    main()
