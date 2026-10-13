"""End-of-turn detection for live calls (32 ms frames)."""

from app.turns import TurnConfig, TurnDetector

LOUD, QUIET = 0.08, 0.004


def run(detector, frames):
    """frames: list of (prob, level, count). Returns the non-None events in order."""
    events = []
    for prob, level, count in frames:
        for _ in range(count):
            e = detector.push(prob, level)
            if e:
                events.append(e)
    return events


def test_speech_then_silence_gives_start_speculate_end():
    d = TurnDetector(TurnConfig(start_ms=160, speculate_ms=256, end_silence_ms=512))
    events = run(d, [(0.9, LOUD, 30), (0.05, QUIET, 20)])
    assert events == ["start", "speculate", "end"]
    assert not d.in_speech


def test_a_short_blip_does_not_start_a_turn():
    d = TurnDetector(TurnConfig(start_ms=160))
    assert run(d, [(0.9, LOUD, 3), (0.05, QUIET, 30)]) == []


def test_quiet_background_voice_never_starts_a_turn():
    d = TurnDetector(TurnConfig(min_level=0.01))
    assert run(d, [(0.95, QUIET, 100)]) == []


def test_pause_shorter_than_end_silence_keeps_the_turn_open():
    d = TurnDetector(TurnConfig(speculate_ms=256, end_silence_ms=512))
    events = run(d, [(0.9, LOUD, 20), (0.05, QUIET, 10), (0.9, LOUD, 20), (0.05, QUIET, 20)])
    assert events == ["start", "speculate", "resume", "speculate", "end"]


def test_long_monologue_is_cut_at_max_turn():
    d = TurnDetector(TurnConfig(max_turn_ms=1600))
    events = run(d, [(0.9, LOUD, 100)])
    assert events[:2] == ["start", "end"]


def test_barge_in_needs_stronger_and_longer_speech_while_agent_speaks():
    cfg = TurnConfig(start_ms=160, barge_in_ms=320, barge_in_prob=0.8, barge_in_level_factor=2.0, min_level=0.01)
    d = TurnDetector(cfg)
    d.agent_speaking = True
    assert run(d, [(0.9, 0.015, 30)]) == []          # echo-level audio: ignored
    assert run(d, [(0.9, LOUD, 6)]) == []            # loud but too short for a barge-in
    assert run(d, [(0.05, QUIET, 5), (0.9, LOUD, 10)]) == ["start"]


def test_reset_clears_state():
    d = TurnDetector()
    run(d, [(0.9, LOUD, 20)])
    d.reset()
    assert not d.in_speech
