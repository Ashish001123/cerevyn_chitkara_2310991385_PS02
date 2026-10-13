"""Live call pipeline: streamed audio -> turn -> STT -> dialogue -> sentence-by-sentence TTS."""

import base64
import io
import wave
from pathlib import Path

import numpy as np
import pytest

from app.call import FRAME, CallSession, StreamingVAD, split_sentences
from tests.conftest import ScriptedSTT, SilentTTS

ROOT = Path(__file__).resolve().parent.parent
SR = 16000


def load(name):
    with wave.open(str(ROOT / "samples/audio" / f"{name}.wav")) as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768


def pcm(audio):
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


def with_silence(audio, before=0.4, after=1.0):
    return np.concatenate([np.zeros(int(before * SR), np.float32), audio, np.zeros(int(after * SR), np.float32)])


def feed_all(session, audio, chunk=FRAME * 4):
    events = []
    data = pcm(audio)
    for i in range(0, len(data), chunk * 2):
        events += session.feed(data[i:i + chunk * 2])
    return events


# -------------------------------------------------------------- sentence split
def test_split_sentences_keeps_titles_together():
    text = "Haircut with Dr. Meera Iyer, Tuesday 20 October at 10 AM. Shall I book it for Priya? Anything else!"
    assert split_sentences(text) == ["Haircut with Dr. Meera Iyer, Tuesday 20 October at 10 AM.",
                                     "Shall I book it for Priya?", "Anything else!"]


def test_split_single_sentence():
    assert split_sentences("Which day?") == ["Which day?"]


# ------------------------------------------------------------- streaming VAD
def test_streaming_vad_finds_speech_and_silence_frame_by_frame():
    vad = StreamingVAD()
    speech = load("s1_t1")
    probs_speech = [vad.prob(speech[i:i + FRAME]) for i in range(0, len(speech) - FRAME, FRAME)]
    vad.reset()
    silence = np.zeros(SR, np.float32)
    probs_silence = [vad.prob(silence[i:i + FRAME]) for i in range(0, len(silence) - FRAME, FRAME)]
    assert max(probs_speech) > 0.8 and np.mean(np.array(probs_speech) > 0.5) > 0.4
    assert max(probs_silence) < 0.2


# -------------------------------------------------------------- call session
@pytest.fixture
def call(dm, tmp_path):
    stt = ScriptedSTT()
    session_id, *_ = dm.start_session()
    return CallSession(dm, stt, SilentTTS(), session_id, tmp_path), stt


def test_spoken_turn_is_detected_transcribed_and_answered(call):
    session, stt = call
    events = feed_all(session, with_silence(load("s1_t1")))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "user_speech_start"
    assert kinds.count("_turn") == 1
    turn = next(e for e in events if e["type"] == "_turn")
    assert 3.0 < len(turn["audio"]) / SR < 7.5          # the whole utterance, not a fragment

    stt.queue.append("Hi, I would like to book a haircut on October 20th at 10 AM. My name is Priya Sharma.")
    out = list(session.respond(turn["audio"]))
    kinds = [e["type"] for e in out]
    assert kinds[:2] == ["transcript", "agent_text"]
    assert out[1]["state"]["step"] == "awaiting_booking_confirmation"
    audio_events = [e for e in out if e["type"] == "agent_audio"]
    assert len(audio_events) == len(split_sentences(out[1]["text"])) >= 2
    assert audio_events[-1]["last"] and not audio_events[0]["last"]
    wav = base64.b64decode(audio_events[0]["audio_b64"])
    assert wave.open(io.BytesIO(wav)).getframerate() == SR
    assert out[-1]["type"] == "timings" and out[-1]["stt_ms"] >= 0


def test_background_noise_alone_never_starts_a_turn(call):
    session, _ = call
    rng = np.random.default_rng(0)
    events = feed_all(session, (rng.standard_normal(3 * SR) * 0.003).astype(np.float32))
    assert events == []


def test_empty_transcript_after_real_speech_asks_to_repeat(call):
    session, stt = call
    stt.queue.append("")
    out = list(session.respond(np.zeros(SR, np.float32)))
    assert out[0]["type"] == "agent_text" and "say that again" in out[0]["text"]


def test_cancel_stops_remaining_sentences(call):
    session, stt = call
    stt.queue.append("Haircut on October 20th at 10 AM, my name is Priya Sharma.")
    seen = []
    for e in session.respond(np.zeros(SR, np.float32)):
        seen.append(e["type"])
        if e["type"] == "agent_audio":
            session.cancel_speech()
    assert seen.count("agent_audio") == 1


def test_dialogue_state_is_shared_with_the_http_api(call, repo):
    session, stt = call
    stt.queue.append("Haircut on October 20th at 10 AM, my name is Priya Sharma.")
    list(session.respond(np.zeros(SR, np.float32)))
    assert repo.load_state(session.session_id)["pending_slot_id"] == "S06"


# ------------------------------------------------------------- websocket (e2e)
def test_websocket_call_from_greeting_to_confirmation_prompt(api):
    # One transcript per STT call: the pause mid-sentence triggers a speculative run that is discarded.
    api.stt.queue.extend(["Hi, I would like to book a haircut on October 20th at 10 AM. My name is Priya Sharma."] * 4)
    with api.websocket_connect("/ws/calls") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "session" and hello["session_id"]
        greeting = ws.receive_json()
        assert greeting["type"] == "agent_text"
        while ws.receive_json()["type"] != "timings":       # greeting audio sentences
            pass
        audio = pcm(with_silence(load("s1_t1")))
        for i in range(0, len(audio), FRAME * 2 * 4):
            ws.send_bytes(audio[i:i + FRAME * 2 * 4])
        events = []
        while not events or events[-1]["type"] not in ("timings", "error"):
            events.append(ws.receive_json())
        kinds = [e["type"] for e in events]
        assert "user_speech_start" in kinds and "transcript" in kinds
        reply = next(e for e in events if e["type"] == "agent_text")
        assert reply["state"]["step"] == "awaiting_booking_confirmation"
        assert any(e["type"] == "agent_audio" for e in events)
        ws.send_json({"type": "hangup"})
