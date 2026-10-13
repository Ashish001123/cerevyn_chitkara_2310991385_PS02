"""Noise-robustness evaluation for the STT + understanding pipeline.

Mixes the sample requests with background babble (other voices) and fan-like
noise at several signal-to-noise ratios, plus background-only clips, then
reports:
  - field accuracy: service / date / time / yes-no / booking ID still extracted correctly
  - false triggers: background-only clips that produced any transcript
  - STT latency

Usage:  python scripts/eval_noise.py [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
import wave
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import nlu  # noqa: E402
from app.config import ROOT, Settings, make_clock  # noqa: E402
from app.db import seed_database  # noqa: E402
from app.repository import Repository  # noqa: E402
from app.speech import WhisperSTT, stt_vocabulary  # noqa: E402

SR = 16000
TODAY = date(2026, 10, 11)
SAMPLES = ROOT / "samples" / "audio"
# utterance id -> fields its transcript must still yield under noise
TARGETS = {
    "s1_t1": {"service": "haircut", "date": "2026-10-20", "time": "10:00"},
    "s2_t1": {"service": "dental checkup", "date": "2026-10-20", "time": "10:00"},
    "s5a_t1": {"service": "physiotherapy", "date": "2026-10-21", "time": "10:00"},
    "s6_t1": {"service": "haircut", "date": "2026-10-21", "time": "15:00"},
    "s3_t2": {"service": "physiotherapy"},
    "s3_t4": {"date": "2026-10-20"},
    "s3_t5": {"time": "14:00"},
    "s1_t2": {"yes": True},
    "s6_t2": {"yes": False},
    "s4_t2": {"booking_id": "BK24017"},
}
BABBLE_SOURCES = ["s3_t1", "s4_t1", "s5b_t1", "s2_t2", "s3_t6", "s4_t3"]


def load(name: str) -> np.ndarray:
    with wave.open(str(SAMPLES / f"{name}.wav")) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768


def save(path: Path, audio: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2)) + 1e-9)


def babble(length: int, rng: np.random.Generator) -> np.ndarray:
    """Several other voices talking at once, like a busy room."""
    out = np.zeros(length, dtype=np.float32)
    for name in rng.choice(BABBLE_SOURCES, size=3, replace=False):
        voice = load(name)
        src = np.tile(voice, length // len(voice) + 2)
        start = rng.integers(0, len(src) - length)
        out += src[start:start + length]
    return out


def fan_noise(length: int, rng: np.random.Generator) -> np.ndarray:
    """Low-frequency-heavy (brown-ish) noise, like a fan or AC."""
    brown = np.cumsum(rng.standard_normal(length).astype(np.float32))
    brown -= np.convolve(brown, np.ones(400) / 400, mode="same")
    return brown / (np.abs(brown).max() + 1e-9)


def mix(speech: np.ndarray, noise: np.ndarray | None, snr_db: float | None) -> np.ndarray:
    pad = np.zeros(int(0.5 * SR), dtype=np.float32)  # room tone before/after, as on a real mic
    s = np.concatenate([pad, speech, pad])
    if noise is None:
        return s
    n = noise[:len(s)]
    return s + n * (rms(speech) / (rms(n) * 10 ** (snr_db / 20)))


def fields(text: str, services: list[dict]) -> dict:
    norm, digits = nlu.prepare(text)
    d, t = nlu.parse_datetime(digits, TODAY)
    return {
        "service": nlu.parse_service(norm, services),
        "date": d.isoformat() if d else None,
        "time": t,
        "yes": nlu.parse_yes_no(norm, "book"),
        "booking_id": nlu.parse_booking_id(digits, expecting=True),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    rng = np.random.default_rng(7)
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / "eval.db"
        seed_database(db, settings.seed_path, reset=True)
        services = Repository(db, make_clock("2026-10-11T09:00:00+05:30")).services()
        stt = WhisperSTT(settings.whisper_model, stt_vocabulary(services))
        stt.warm_up()

        results, latencies = {}, []
        for condition, snr in [("clean", None), ("babble_20dB", 20), ("babble_10dB", 10), ("babble_5dB", 5),
                               ("fan_10dB", 10), ("fan_5dB", 5)]:
            ok = total = 0
            failures = []
            for uid, expected in TARGETS.items():
                speech = load(uid)
                length = len(speech) + SR
                noise = None if snr is None else (
                    babble(length, rng) if condition.startswith("babble") else fan_noise(length, rng))
                path = save(tmp / f"{uid}_{condition}.wav", mix(speech, noise, snr))
                t0 = time.perf_counter()
                text = stt.transcribe(path)
                latencies.append(time.perf_counter() - t0)
                got = fields(text, services)
                for k, v in expected.items():
                    total += 1
                    if got[k] == v:
                        ok += 1
                    else:
                        failures.append(f"{uid}.{k}: want {v!r} got {got[k]!r}  <- {text!r}")
            results[condition] = {"field_accuracy": round(ok / total, 3), "failures": failures}
            print(f"{condition:12} field accuracy {ok}/{total} = {ok / total:.0%}")
            for f in failures:
                print("    ", f)

        # Background-only clips: distant voices or a fan, nobody talking to the agent.
        triggers = []
        for i in range(10):
            length = 4 * SR
            noise = babble(length, rng) if i % 2 == 0 else fan_noise(length, rng)
            noise = noise * (0.006 / rms(noise))  # distant background, about -44 dBFS
            text = stt.transcribe(save(tmp / f"bg_{i}.wav", noise))
            if text:
                triggers.append(text)
        results["background_only"] = {"false_triggers": len(triggers), "clips": 10, "texts": triggers}
        print(f"background-only false triggers: {len(triggers)}/10 {triggers}")
        lat = sorted(latencies)
        results["stt_latency_s"] = {"median": round(statistics.median(lat), 3), "p90": round(lat[int(0.9 * len(lat))], 3)}
        print("STT latency:", results["stt_latency_s"])
    if args.json:
        args.json.write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
