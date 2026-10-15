# TDD Evidence: Call Quality (dates/times, replies, noise, live call)

**Source plan:** the `/ecc:plan` "Real-Time, Noise-Robust Voice Call Agent" (inline in the session,
not a file). The user didn't answer the plan's open questions, so these defaults were used: no Azure
key (local speech), no LLM fallback (no Anthropic key, Phase 3 skipped), browser only, existing
Python 3.14 venv. The project is not a git repository, so there are no checkpoint commits; this
report holds the RED/GREEN evidence.

## User journeys

1. As a caller, I want to say dates and times the way people speak ("the 20th at 10", "ten thirty",
   "1400"), so the agent books the slot I meant and never invents a different date.
2. As a caller, I want short replies that sound like a person, so the call moves quickly.
3. As a caller in a noisy room, I want background voices and hum ignored, so only what I say is
   transcribed and acted on.
4. As a caller, I want to just talk, with the agent detecting when I finish, answering immediately and
   stopping when I interrupt.

## Task report

### Phase 1: Date/time grammar (`app/temporal.py`)
- **Summary:** replaced the regex date/time code in `nlu.py` with a context-aware grammar. The
  dialogue passes `expecting=state["awaiting"]`.
- **RED:** `pytest tests/test_temporal.py` gave `53 failed, 1 passed`, all `TypeError` (missing
  `expecting` parameter). After adding the parameter only, it gave **`30 failed, 24 passed`**. Each
  failure was real missing behavior, for example `20th October 1400` returning year 1400, `10.30 am`
  returning no time, and `the 20th at 10` returning no date.
- **GREEN:** `54 passed`. Dialogue test `test_bare_day_number_answers_which_date`: RED `1 failed,
  15 passed`, then GREEN after the one-line dialogue change. Full suite: `129 passed`.
- **Guaranteed:** 32 full phrases and 12 short answers resolve correctly; booking IDs, names and bare
  numbers are never misread as dates or times when no date or time was asked for; invalid values
  (`October 32nd`, `10:75`, `25 30`) are rejected rather than guessed.

### Phase 5: Short, natural replies (`app/dialogue.py`)
- **Summary:** natural time and date speech ("10 AM", "Tuesday 20 October"); the year only when it
  isn't the current year; alternatives grouped by day; IST said once, in the booking confirmation.
- **RED:** `pytest tests/test_responses.py` gave **`9 failed, 2 passed`**.
- **GREEN:** `11 passed`. Full suite `140 passed`. One existing assertion
  (`"10:00 AM, 11:00 AM, 2:00 PM"`) was updated as an intended spec change.
- **Guaranteed:** the confirmation still reads back the exact slot and ends with a question; the
  booking confirmation keeps the ID and the full time with IST; slot-filling prompts are at most 30
  words.

### Phase 2: Background voices and hallucinations (`app/speech.py`)
- **Baseline** (`scripts/eval_noise.py`, saved as `docs/eval/noise_baseline.json`): field accuracy
  100% in every condition, but **5/10 background-only clips produced transcripts** ("I want to cancel
  my booking", "For me on October 11th at 10 AM", "Yes, we can see it").
- **RED:** `pytest tests/test_speech_guards.py tests/test_nlu.py` gave **`23 failed, 49 passed`**. The
  real-model test failed with `assert 'I would like...Vikram Singh.' == ''`: distant chatter
  transcribed as a booking.
- **GREEN:** `166 passed`.
- **Regression found by the eval:** "2 PM." became "Tufian". The cause was tighter VAD padding
  (200 ms). Restoring 400 ms padding fixed it.
- **Prompt echo:** RED `1 failed` ("Appointment booking call." returned as a transcript), then GREEN.
- **Final eval** (`docs/eval/noise_final.json`): background-only false triggers **0/10**; field
  accuracy 100% for clean, babble 20/10 dB and fan 10/5 dB; 94% at three-voice babble 5 dB (the miss
  produces no field, so the agent asks again).

### Phase 4: Live full-duplex call (`app/turns.py`, `app/call.py`, `/ws/calls`, UI)
- **RED:** `ModuleNotFoundError: app.turns` / `app.call` at collection. This is a compile-time RED
  for the missing implementation.
- **GREEN:** `tests/test_turns.py` `7 passed`; `tests/test_call.py -k "not websocket"` `8 passed`.
- **Bug found by the WebSocket test:** the test hung. The root cause was a mid-sentence pause that
  triggered a speculative STT run, so the final STT call raised in a worker thread and the server
  swallowed the error. Fix: response failures are now logged and sent to the client as an `error`
  event. The test now scripts one transcript per STT call. Full suite: `183 passed`.
- **Live measurement** (sample WAVs streamed in real time to the running server): end of speech to
  first reply audio is **1168 ms** for a 5.5 s request and **839 ms** for "Yes, please". The
  speculative transcript was reused, so STT on the critical path was 0 ms.

## Test specification

| # | What is guaranteed | Test | Type | Result |
|---|---|---|---|---|
| 1 | Numeric/spoken dates and times resolve correctly; 1400 is never a year | `tests/test_temporal.py` (54) | unit | PASS |
| 2 | Bare "20" after "which day?" and "1400" after "what time?" fill the right slot | `tests/test_dialogue.py::test_bare_day_number_answers_which_date` | integration | PASS |
| 3 | Military time in a full sentence books 2 PM, not year 1400 | `test_military_time_is_not_read_as_a_year` | integration | PASS |
| 4 | Replies are short, the year is omitted, IST said once, exact slot read back before yes/no | `tests/test_responses.py` (11) | unit/integration | PASS |
| 5 | Quiet or distant speech segments are dropped; caller-level segments kept | `test_speech_guards.py` level tests | unit | PASS |
| 6 | Low-confidence, looping, prompt-echo and known hallucination text is discarded | `test_speech_guards.py` cleaning tests | unit | PASS |
| 7 | Background chatter alone produces no transcript (real Whisper) | `test_background_voices_alone_do_not_produce_a_transcript` | integration | PASS |
| 8 | Caller is transcribed correctly over babble 10 dB below (real Whisper) | `test_caller_is_transcribed_over_background_babble` | integration | PASS |
| 9 | Hinglish/Indian-English yes/no understood | `test_parse_yes_no_indian_english_and_hinglish` (11) | unit | PASS |
| 10 | Turns start only on sustained, loud-enough speech; end after 500 ms; speculate/resume; barge-in needs stronger speech | `tests/test_turns.py` (7) | unit | PASS |
| 11 | Streaming Silero VAD separates speech from silence frame by frame | `test_streaming_vad_finds_speech_and_silence_frame_by_frame` | integration | PASS |
| 12 | A streamed utterance becomes one turn; reply arrives as transcript → text → per-sentence audio → timings | `test_spoken_turn_is_detected_transcribed_and_answered` | integration | PASS |
| 13 | Barge-in stops remaining sentences | `test_cancel_stops_remaining_sentences` | unit | PASS |
| 14 | WebSocket call from greeting to the confirmation prompt | `test_websocket_call_from_greeting_to_confirmation_prompt` | e2e | PASS |
| 15 | Slot consistency, confirmation and the demo scenarios are unchanged | `scripts/run_demo.py`: 13/13 checks | e2e | PASS |

## Coverage and known gaps

`pytest --cov=app` gives **85% total** (184 tests): `turns.py` 100%, `nlu.py` 98%, `temporal.py`
97%, `call.py` 97%, `dialogue.py` 88%, `main.py` 81%, `speech.py` 52%.

`speech.py` is lower because the Azure STT/TTS classes can't run without a key, and the macOS
native, `say` and pyttsx3 TTS engines are platform-specific (they are exercised by the demo and the
live server, not by unit tests).

Known gaps:
- Three-voice babble at 5 dB still loses one short answer (the agent re-prompts rather than acting).
- Separating background voices relies on loudness; there is no speaker verification.
- The browser AudioWorklet/playback code has no automated browser test. It was syntax-checked with
  `node --check`, and the server side is covered by the WebSocket e2e test.
- Phase 3 (LLM fallback) and Phase 6 (Azure streaming SDK) were not implemented.
