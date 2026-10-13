"""Background-voice and hallucination guards around speech-to-text."""

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.speech import clean_transcript, foreground_segments

SR = 16000
ROOT = Path(__file__).resolve().parent.parent


def tone(seconds, amplitude, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * SR)) * amplitude).astype(np.float32)


# ------------------------------------------------------------ level gating
def test_quiet_background_segment_is_dropped_when_caller_is_louder():
    audio = np.concatenate([tone(1, 0.10), np.zeros(SR, np.float32), tone(1, 0.02, seed=1)])
    segments = [{"start": 0, "end": SR}, {"start": 2 * SR, "end": 3 * SR}]
    assert foreground_segments(audio, segments) == [{"start": 0, "end": SR}]


def test_segments_at_similar_level_are_kept():
    audio = np.concatenate([tone(1, 0.10), tone(1, 0.07, seed=1)])
    segments = [{"start": 0, "end": SR}, {"start": SR, "end": 2 * SR}]
    assert foreground_segments(audio, segments) == segments


def test_distant_speech_alone_is_dropped_by_absolute_floor():
    audio = tone(2, 0.006)
    assert foreground_segments(audio, [{"start": 0, "end": 2 * SR}]) == []


def test_no_segments_means_nothing_to_transcribe():
    assert foreground_segments(tone(1, 0.1), []) == []


# ---------------------------------------------------- transcript cleaning
def seg(text, no_speech=0.05, logprob=-0.3, ratio=1.4):
    return SimpleNamespace(text=text, no_speech_prob=no_speech, avg_logprob=logprob, compression_ratio=ratio)


def test_confident_segments_are_kept():
    assert clean_transcript([seg(" Book a haircut"), seg(" on October 20th.")]) == "Book a haircut on October 20th."


def test_low_confidence_non_speech_segment_is_dropped():
    assert clean_transcript([seg(" Yes please."), seg(" Thank you.", no_speech=0.8, logprob=-1.2)]) == "Yes please."


def test_repetitive_segment_is_dropped():
    looped = " I want to cancel my booking, I want to cancel my booking, I want to cancel my booking."
    assert clean_transcript([seg(looped, ratio=2.9)]) == ""


@pytest.mark.parametrize("text", ["Thank you.", "Thanks for watching!", "you", "Bye.", "[Music]", "..."])
def test_known_noise_hallucinations_are_dropped(text):
    assert clean_transcript([seg(" " + text)]) == ""


def test_real_short_answers_survive():
    for text in ("Yes.", "No.", "2 PM.", "Haircut."):
        assert clean_transcript([seg(" " + text)]) == text


# ---------------------------------------------------- real model (slow)
def _model_cached():
    return any((Path.home() / ".cache/huggingface/hub").glob("models--Systran--faster-whisper-small.en"))


@pytest.mark.skipif(not _model_cached(), reason="whisper small.en not downloaded")
def test_background_voices_alone_do_not_produce_a_transcript(tmp_path):
    import wave

    from app.speech import WhisperSTT

    def load(name):
        with wave.open(str(ROOT / "samples/audio" / f"{name}.wav")) as w:
            return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768

    voices = [load(n) for n in ("s3_t1", "s4_t1", "s5b_t1")]
    n = max(len(v) for v in voices)
    room = sum(np.pad(v, (0, n - len(v))) for v in voices)
    room *= 0.006 / np.sqrt(np.mean(room ** 2))           # distant chatter, about -44 dBFS
    path = tmp_path / "room.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((room * 32767).astype("<i2").tobytes())
    assert WhisperSTT("small.en").transcribe(path) == ""


def test_echo_of_the_vocabulary_prompt_is_not_a_transcript():
    from app.speech import is_prompt_echo
    prompt = "Appointment booking call. Services: haircut at Urban Trim Salon. Booking ID BK followed by five digits."
    assert is_prompt_echo("Appointment booking call.", prompt)
    assert is_prompt_echo("Booking ID BK followed by five digits", prompt)
    assert not is_prompt_echo("Book a haircut.", prompt)
    assert not is_prompt_echo("2 PM.", prompt)
    assert not is_prompt_echo("Haircut", prompt)          # a one-word answer that happens to be in the prompt


@pytest.mark.skipif(not _model_cached(), reason="whisper small.en not downloaded")
def test_caller_is_transcribed_over_background_babble(tmp_path):
    import wave

    from app import nlu
    from app.speech import WhisperSTT

    def load(name):
        with wave.open(str(ROOT / "samples/audio" / f"{name}.wav")) as w:
            return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768

    caller = load("s1_t1")
    babble = np.tile(load("s4_t1") + 0, 4)[: len(caller)]
    babble *= np.sqrt(np.mean(caller ** 2)) / (np.sqrt(np.mean(babble ** 2)) * 10 ** (10 / 20))   # 10 dB below
    path = tmp_path / "noisy.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(caller + babble, -1, 1) * 32767).astype("<i2").tobytes())
    text = WhisperSTT("small.en").transcribe(path)
    norm, digits = nlu.prepare(text)
    assert "haircut" in norm
    from datetime import date
    assert nlu.parse_datetime(digits, date(2026, 10, 11)) == (date(2026, 10, 20), "10:00")
