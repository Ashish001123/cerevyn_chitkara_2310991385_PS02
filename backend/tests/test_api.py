import io
import wave


def wav_bytes():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)
    return buf.getvalue()


def voice(api, session_id, transcript):
    api.stt.queue.append(transcript)
    r = api.post(f"/sessions/{session_id}/voice", files={"audio": ("turn.wav", wav_bytes(), "audio/wav")})
    assert r.status_code == 200, r.text
    return r.json()


def test_voice_booking_flow_returns_audio_and_booking_id(api):
    start = api.post("/sessions").json()
    assert start["reply_audio_url"].startswith("/audio/")
    sid = start["session_id"]
    r = voice(api, sid, "Book a haircut on October 20th at 10 AM. My name is Priya Sharma.")
    assert r["state"]["step"] == "awaiting_booking_confirmation"
    assert api.get(r["reply_audio_url"]).status_code == 200
    r = voice(api, sid, "Yes please")
    booking_id = r["state"]["last_booking_id"]
    assert api.get(f"/bookings/{booking_id}").json()["booking_status"] == "confirmed"
    turns = api.get(f"/sessions/{sid}").json()["turns"]
    assert [t["role"] for t in turns] == ["agent", "user", "agent", "user", "agent"]
    assert turns[1]["audio_path"] and turns[2]["audio_path"]


def test_slots_endpoint_lists_only_available(api):
    slots = api.get("/slots", params={"service": "dental checkup", "date": "2026-10-20"}).json()
    assert [s["slot_time"] for s in slots] == ["11:00", "14:00"]


def test_audit_endpoint_records_changes(api):
    sid = api.post("/sessions").json()["session_id"]
    api.post(f"/sessions/{sid}/text", json={"text": "cancel booking BK24017", "speak": False})
    api.post(f"/sessions/{sid}/text", json={"text": "yes", "speak": False})
    actions = [a["action"] for a in api.get("/audit").json()]
    assert actions[-1] == "CANCELLED"


def test_errors(api):
    assert api.post("/sessions/nope/text", json={"text": "hi"}).status_code == 404
    sid = api.post("/sessions").json()["session_id"]
    r = api.post(f"/sessions/{sid}/voice", files={"audio": ("x.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 415
    assert api.get("/bookings/BK00000").status_code == 404
