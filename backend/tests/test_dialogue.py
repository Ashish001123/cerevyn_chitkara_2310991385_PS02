from app.dialogue import DialogueManager


def confirmed_bookings(repo, slot_id):
    return [b for b in repo.list_bookings() if b["slot_id"] == slot_id and b["booking_status"] == "confirmed"]


def test_successful_booking_requires_confirmation(convo, repo):
    c = convo()
    reply = c.say("Hi, I would like to book a haircut on October 20th at 10 AM. My name is Priya Sharma.")
    assert "Shall I book it" in reply
    assert c.state["step"] == "awaiting_booking_confirmation"
    assert repo.get_slot("S06")["status"] == "available"      # nothing booked before "yes"
    reply = c.say("Yes, please.")
    booking_id = c.state["last_booking_id"]
    assert booking_id in reply and booking_id.startswith("BK")
    assert repo.get_booking(booking_id)["slot_id"] == "S06"


def test_unavailable_slot_offers_only_real_alternatives(convo, repo):
    c = convo()
    reply = c.say("I need a dental checkup on October 20th at 10 AM. My name is Rohan Gupta.")
    assert "already booked" in reply
    assert c.state["step"] == "offering_alternatives"
    assert c.state["alternatives"] == ["S02", "S03", "S04"]
    for slot_id in c.state["alternatives"]:
        assert repo.get_slot(slot_id)["status"] == "available"
    c.say("The 11 AM one, please.")
    assert c.state["pending_slot_id"] == "S02"
    c.say("Yes, book it.")
    assert confirmed_bookings(repo, "S02")[0]["customer_name"] == "Rohan Gupta"


def test_time_that_is_not_a_slot_is_unavailable(convo):
    c = convo()
    reply = c.say("Haircut on October 20th at 10:30 AM, my name is Asha.")
    assert "There is no haircut slot" in reply
    assert c.state["step"] == "offering_alternatives"


def test_missing_information_is_collected_step_by_step(convo, repo):
    c = convo()
    c.say("I would like to book an appointment.")
    assert c.state["awaiting"] == "service"
    c.say("Physiotherapy, please.")
    assert c.state["awaiting"] == "customer_name"
    c.say("Karan Mehta.")
    assert (c.state["customer_name"], c.state["awaiting"]) == ("Karan Mehta", "date")
    reply = c.say("October 20th.")
    assert c.state["awaiting"] == "time" and "10 AM, 11 AM or 2 PM" in reply
    c.say("2 PM.")
    assert c.state["step"] == "awaiting_booking_confirmation"
    c.say("Hmm, let me think.")
    assert c.state["step"] == "awaiting_booking_confirmation"   # ambiguity is never consent
    assert repo.get_slot("S13")["status"] == "available"
    c.say("Yes.")
    assert confirmed_bookings(repo, "S13")[0]["customer_name"] == "Karan Mehta"


def test_declining_confirmation_books_nothing(convo, repo):
    c = convo()
    c.say("Can I get a haircut on October 21st at 3 PM? This is Meera Joshi.")
    reply = c.say("No, don't book it.")
    assert "not booked anything" in reply
    assert repo.get_slot("S10")["status"] == "available"
    assert c.state["service"] == "haircut" and c.state["customer_name"] == "Meera Joshi"


def test_changing_time_at_confirmation_rechecks_availability(convo, repo):
    c = convo()
    c.say("Haircut on October 20th at 10 AM, my name is Priya Sharma.")
    reply = c.say("Actually make it 2 PM instead.")
    assert "haven't booked anything" in reply
    assert c.state["pending_slot_id"] == "S08"
    assert repo.get_slot("S06")["status"] == "available"


def test_cancellation_with_id_and_confirmation(convo, repo):
    c = convo()
    c.say("I want to cancel my booking.")
    assert c.state["awaiting"] == "booking_id"
    reply = c.say("My booking ID is B K 2 4 0 1 7.")
    assert "Rahul Verma" in reply and c.state["step"] == "awaiting_cancel_confirmation"
    assert repo.get_booking("BK24017")["booking_status"] == "confirmed"
    c.say("Yes, cancel it.")
    assert repo.get_booking("BK24017")["booking_status"] == "cancelled"
    assert repo.get_slot("S07")["status"] == "available"


def test_cancellation_declined_keeps_booking(convo, repo):
    c = convo()
    c.say("Please cancel booking BK31552")
    reply = c.say("No, keep it.")
    assert "still confirmed" in reply
    assert repo.get_booking("BK31552")["booking_status"] == "confirmed"


def test_unknown_booking_id(convo):
    c = convo()
    c.say("cancel my appointment")
    reply = c.say("12345")
    assert "couldn't find" in reply and c.state["awaiting"] == "booking_id"


def test_two_sessions_same_slot_second_is_rejected(convo, repo):
    a, b = convo(), convo()
    a.say("I want to book physiotherapy on October 21st at 10 AM. My name is Sneha Kapoor.")
    b.say("Book physiotherapy for me on October 21st at 10 AM. My name is Vikram Singh.")
    assert a.state["pending_slot_id"] == b.state["pending_slot_id"] == "S14"
    a.say("Yes, please book it.")
    reply = b.say("Yes, confirm.")
    assert "just booked by another customer" in reply
    assert b.state["step"] == "offering_alternatives" and "S14" not in b.state["alternatives"]
    assert [x["customer_name"] for x in confirmed_bookings(repo, "S14")] == ["Sneha Kapoor"]
    b.say("The first one.")
    b.say("Yes.")
    assert repo.get_booking(b.state["last_booking_id"])["slot_id"] != "S14"


def test_alternative_taken_before_choice_is_rechecked(convo, repo):
    c = convo()
    c.say("Dental checkup on October 20th at 10 AM, my name is Rohan Gupta.")
    repo.reserve_slot("S02", "Someone Faster", None)
    reply = c.say("option one")
    assert "just been taken" in reply and "S02" not in c.state["alternatives"]


def test_dialogue_state_survives_restart(repo, clock):
    first = DialogueManager(repo, clock)
    session_id, *_ = first.start_session()
    first.handle(session_id, "I'd like a haircut. My name is Priya Sharma.")
    restarted = DialogueManager(repo, clock)                  # fresh process, same database
    reply, state, _ = restarted.handle(session_id, "October 20th at 10 AM")
    assert state["step"] == "awaiting_booking_confirmation"
    assert state["customer_name"] == "Priya Sharma" and state["service"] == "haircut"
    roles = [t["role"] for t in repo.turns(session_id)]
    assert roles == ["agent", "user", "agent", "user", "agent"]


def test_start_over_resets_state(convo):
    c = convo()
    c.say("Haircut on October 20th at 10 AM, my name is Priya Sharma.")
    c.say("start over")
    assert c.state["step"] == "start" and c.state["service"] is None


# ----------------------------------------------------- spoken numbers in context
def test_bare_day_number_answers_which_date(convo):
    c = convo()
    c.say("I'd like physiotherapy. My name is Karan Mehta.")
    assert c.state["awaiting"] == "date"
    c.say("20")
    assert c.state["date"] == "2026-10-20" and c.state["awaiting"] == "time"
    c.say("1400")
    assert c.state["pending_slot_id"] == "S13"


def test_military_time_is_not_read_as_a_year(convo):
    c = convo()
    c.say("Physiotherapy on 20th October 1400. My name is Karan Mehta.")
    assert c.state["pending_slot_id"] == "S13"
    assert c.state["date"] == "2026-10-20" and c.state["time"] == "14:00"


def test_day_without_month_and_colon_free_time(convo):
    c = convo()
    c.say("Haircut on the 20th at 10 30, my name is Priya Sharma.")
    assert c.state["date"] == "2026-10-20" and c.state["time"] == "10:30"
    assert c.state["step"] == "offering_alternatives"        # no 10:30 slot: real alternatives only
    c.say("the 21st")
    assert c.state["date"] == "2026-10-21"
