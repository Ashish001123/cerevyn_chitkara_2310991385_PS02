# PS02 Voice Agent: Demo Report

Clock fixed at `2026-10-11T09:00:00+05:30` (Asia/Kolkata). STT: `faster-whisper`, TTS: `macos-native`.

Every user turn is a WAV file uploaded to `POST /sessions/{id}/voice`, transcribed by the STT engine, handled by the dialogue manager against SQLite, and answered with synthesized audio.

## Successful booking

| Who | Audio | Text | Dialogue step |
|---|---|---|---|
| Agent → Priya |  | Hi! I can book or cancel a dental checkup, haircut or physiotherapy appointment. How can I help? | `start` |
| Priya | `samples/audio/s1_t1.wav` | STT: “Hi, I would like to book a haircut on October 20th 10 AM. My name is Priya Sarma.” | |
| Agent → Priya | `demo_output/audio/s1_t1_reply.wav` | Haircut with Arjun Nair at Urban Trim Salon, Tuesday 20 October at 10 AM. Shall I book it for Priya? | `awaiting_booking_confirmation` |
| Priya | `samples/audio/s1_t2.wav` | STT: “Yes please."” | |
| Agent → Priya | `demo_output/audio/s1_t2_reply.wav` | You're confirmed! Your booking ID is BK96180. Haircut with Arjun Nair at Urban Trim Salon, Tuesday 20 October at 10 AM IST. Anything else? | `start` |

## Unavailable slot with alternatives from the database

| Who | Audio | Text | Dialogue step |
|---|---|---|---|
| Agent → Rohan |  | Hi! I can book or cancel a dental checkup, haircut or physiotherapy appointment. How can I help? | `start` |
| Rohan | `samples/audio/s2_t1.wav` | STT: “I need a dental checkup on the 20th of October at 10 AM. My name is Rohan Gupta.” | |
| Agent → Rohan | `demo_output/audio/s2_t1_reply.wav` | Sorry, Tuesday 20 October at 10 AM is already booked. I have 11 AM or 2 PM on Tuesday 20 October, or 10 AM on Wednesday 21 October. Which works for you? | `offering_alternatives` |
| Rohan | `samples/audio/s2_t2.wav` | STT: “The 11 AM one, please.” | |
| Agent → Rohan | `demo_output/audio/s2_t2_reply.wav` | Dental checkup with Dr. Meera Iyer at Smile Dental Clinic, Tuesday 20 October at 11 AM. Shall I book it for Rohan? | `awaiting_booking_confirmation` |
| Rohan | `samples/audio/s2_t3.wav` | STT: “Yes, book it.” | |
| Agent → Rohan | `demo_output/audio/s2_t3_reply.wav` | You're confirmed! Your booking ID is BK65978. Dental checkup with Dr. Meera Iyer at Smile Dental Clinic, Tuesday 20 October at 11 AM IST. Anything else? | `start` |

## Missing information collected over several turns (with an ambiguous answer)

| Who | Audio | Text | Dialogue step |
|---|---|---|---|
| Agent → Karan |  | Hi! I can book or cancel a dental checkup, haircut or physiotherapy appointment. How can I help? | `start` |
| Karan | `samples/audio/s3_t1.wav` | STT: “I would like to book an appointment.” | |
| Agent → Karan | `demo_output/audio/s3_t1_reply.wav` | Sure. Which service: dental checkup, haircut or physiotherapy? | `collecting` |
| Karan | `samples/audio/s3_t2.wav` | STT: “Physiotherapy fleet.” | |
| Agent → Karan | `demo_output/audio/s3_t2_reply.wav` | Sure, a physiotherapy appointment. What's your name? | `collecting` |
| Karan | `samples/audio/s3_t3.wav` | STT: “My name is Karan Mehta.” | |
| Agent → Karan | `demo_output/audio/s3_t3_reply.wav` | Thanks, Karan. Which day? I have openings on Tuesday 20 October or Wednesday 21 October. | `collecting` |
| Karan | `samples/audio/s3_t4.wav` | STT: “October 20th.” | |
| Agent → Karan | `demo_output/audio/s3_t4_reply.wav` | What time on Tuesday 20 October? I have 10 AM, 11 AM or 2 PM. | `collecting` |
| Karan | `samples/audio/s3_t5.wav` | STT: “2 p.m.” | |
| Agent → Karan | `demo_output/audio/s3_t5_reply.wav` | Physiotherapy with Dr. Sana Qureshi at Kinetic Physio Centre, Tuesday 20 October at 2 PM. Shall I book it for Karan? | `awaiting_booking_confirmation` |
| Karan | `samples/audio/s3_t6.wav` | STT: “On Let Me Think.” | |
| Agent → Karan | `demo_output/audio/s3_t6_reply.wav` | Sorry, is that a yes or a no? Shall I book Tuesday 20 October at 2 PM? | `awaiting_booking_confirmation` |
| Karan | `samples/audio/s3_t7.wav` | STT: “Yes.” | |
| Agent → Karan | `demo_output/audio/s3_t7_reply.wav` | You're confirmed! Your booking ID is BK16005. Physiotherapy with Dr. Sana Qureshi at Kinetic Physio Centre, Tuesday 20 October at 2 PM IST. Anything else? | `start` |

## Cancellation with booking ID and confirmation

| Who | Audio | Text | Dialogue step |
|---|---|---|---|
| Agent → Rahul |  | Hi! I can book or cancel a dental checkup, haircut or physiotherapy appointment. How can I help? | `start` |
| Rahul | `samples/audio/s4_t1.wav` | STT: “I want to cancel my booking.” | |
| Agent → Rahul | `demo_output/audio/s4_t1_reply.wav` | Sure. What's your booking ID? It starts with B K. | `collecting` |
| Rahul | `samples/audio/s4_t2.wav` | STT: “My booking ID is BK24017.” | |
| Agent → Rahul | `demo_output/audio/s4_t2_reply.wav` | I found BK24017: haircut for Rahul Verma, Tuesday 20 October at 11 AM. Shall I cancel it? | `awaiting_cancel_confirmation` |
| Rahul | `samples/audio/s4_t3.wav` | STT: “Yes, cancel it.” | |
| Agent → Rahul | `demo_output/audio/s4_t3_reply.wav` | Done. Booking BK24017 has been cancelled, and Tuesday 20 October at 11 AM is free again. Anything else? | `start` |

## Two customers try to reserve the same slot

| Who | Audio | Text | Dialogue step |
|---|---|---|---|
| Agent → Sneha |  | Hi! I can book or cancel a dental checkup, haircut or physiotherapy appointment. How can I help? | `start` |
| Agent → Vikram |  | Hi! I can book or cancel a dental checkup, haircut or physiotherapy appointment. How can I help? | `start` |
| Sneha | `samples/audio/s5a_t1.wav` | STT: “I want to book Physiotherapy on the 21st of October at 10 AM. My name is Sneha Kapoor.” | |
| Agent → Sneha | `demo_output/audio/s5a_t1_reply.wav` | Physiotherapy with Dr. Sana Qureshi at Kinetic Physio Centre, Wednesday 21 October at 10 AM. Shall I book it for Sneha? | `awaiting_booking_confirmation` |
| Vikram | `samples/audio/s5b_t1.wav` | STT: “Book Physiotherapy for me on October 21st at 10 AM. My name is Vikram Singh.” | |
| Agent → Vikram | `demo_output/audio/s5b_t1_reply.wav` | Physiotherapy with Dr. Sana Qureshi at Kinetic Physio Centre, Wednesday 21 October at 10 AM. Shall I book it for Vikram? | `awaiting_booking_confirmation` |
| Sneha | `samples/audio/s5a_t2.wav` | STT: “Yes, please book it.” | |
| Agent → Sneha | `demo_output/audio/s5a_t2_reply.wav` | You're confirmed! Your booking ID is BK43146. Physiotherapy with Dr. Sana Qureshi at Kinetic Physio Centre, Wednesday 21 October at 10 AM IST. Anything else? | `start` |
| Vikram | `samples/audio/s5b_t2.wav` | STT: “Yes, confirmed.” | |
| Agent → Vikram | `demo_output/audio/s5b_t2_reply.wav` | Sorry, that slot was just booked by another customer, so I could not reserve it. I have 10 AM or 11 AM on Tuesday 20 October, or 3 PM on Wednesday 21 October. Which works for you? | `offering_alternatives` |
| Vikram | `samples/audio/s5b_t3.wav` | STT: “The first one.” | |
| Agent → Vikram | `demo_output/audio/s5b_t3_reply.wav` | Physiotherapy with Dr. Sana Qureshi at Kinetic Physio Centre, Tuesday 20 October at 10 AM. Shall I book it for Vikram? | `awaiting_booking_confirmation` |
| Vikram | `samples/audio/s5b_t4.wav` | STT: “Yes.” | |
| Agent → Vikram | `demo_output/audio/s5b_t4_reply.wav` | You're confirmed! Your booking ID is BK48757. Physiotherapy with Dr. Sana Qureshi at Kinetic Physio Centre, Tuesday 20 October at 10 AM IST. Anything else? | `start` |

## Customer declines at the confirmation step, nothing is booked

| Who | Audio | Text | Dialogue step |
|---|---|---|---|
| Agent → Meera |  | Hi! I can book or cancel a dental checkup, haircut or physiotherapy appointment. How can I help? | `start` |
| Meera | `samples/audio/s6_t1.wav` | STT: “Can I get a haircut on October 21st at 3 PM? This is Mira Joshi.” | |
| Agent → Meera | `demo_output/audio/s6_t1_reply.wav` | Haircut with Arjun Nair at Urban Trim Salon, Wednesday 21 October at 3 PM. Shall I book it for Mira? | `awaiting_booking_confirmation` |
| Meera | `samples/audio/s6_t2.wav` | STT: “No, don't book it.” | |
| Agent → Meera | `demo_output/audio/s6_t2_reply.wav` | No problem, I have not booked anything. Which other day would you like? | `collecting` |

## Concurrency stress test

```json
{
  "slot": "S09 (haircut, 21 Oct 2026 10:00 IST)",
  "attempts": 20,
  "successes": 1,
  "winner": [
    "BK21306"
  ],
  "confirmed_bookings_for_slot": 1,
  "rejections_audited": 19
}
```

## Checks

- PASS: 1 Successful booking: haircut 20 Oct 10:00 (S06) booked for Priya
- PASS: 2 Unavailable slot: dental 20 Oct 10:00 (S01) still holds only Anita's booking
- PASS: 2 Unavailable slot: offered alternative 20 Oct 11:00 (S02) booked for Rohan
- PASS: 3 Missing info: physiotherapy 20 Oct 14:00 (S13) booked for Karan
- PASS: 4 Cancellation: BK24017 cancelled and slot S07 available again
- PASS: 5 Same slot twice: Sneha got physiotherapy 21 Oct 10:00 (S14)
- PASS: 5 Same slot twice: Vikram's confirmation was rejected and audited
- PASS: 5 Same slot twice: Vikram then booked a different offered slot
- PASS: 6 Declined confirmation: agent asked to confirm S10, customer said no, slot still available
- PASS: Every BOOKED/CANCELLED change followed a yes/no prompt for that exact slot or booking
- PASS: No slot has more than one confirmed booking
- PASS: Booked slots match confirmed bookings exactly
- PASS: Race: 20 concurrent reservations of one slot -> 1 success, 19 audited rejections

## Booking records after the demo

| Booking ID | Customer | Service | Start (IST) | Status |
|---|---|---|---|---|
| BK24017 | Rahul Verma | haircut | 2026-10-20T11:00:00+05:30 | cancelled |
| BK31552 | Anita Desai | dental checkup | 2026-10-20T10:00:00+05:30 | confirmed |
| BK96180 | Priya Sarma | haircut | 2026-10-20T10:00:00+05:30 | confirmed |
| BK65978 | Rohan Gupta | dental checkup | 2026-10-20T11:00:00+05:30 | confirmed |
| BK16005 | Karan Mehta | physiotherapy | 2026-10-20T14:00:00+05:30 | confirmed |
| BK43146 | Sneha Kapoor | physiotherapy | 2026-10-21T10:00:00+05:30 | confirmed |
| BK48757 | Vikram Singh | physiotherapy | 2026-10-20T10:00:00+05:30 | confirmed |

## Audit log

| # | Time | Action | Booking | Slot | Actor |
|---|---|---|---|---|---|
| 1 | 2026-10-09T18:30:00+05:30 | SEEDED | BK24017 | S07 | system |
| 2 | 2026-10-10T11:15:00+05:30 | SEEDED | BK31552 | S01 | system |
| 3 | 2026-10-11T09:00:00+05:30 | BOOKED | BK96180 | S06 | Priya Sarma |
| 4 | 2026-10-11T09:00:00+05:30 | BOOKED | BK65978 | S02 | Rohan Gupta |
| 5 | 2026-10-11T09:00:00+05:30 | BOOKED | BK16005 | S13 | Karan Mehta |
| 6 | 2026-10-11T09:00:00+05:30 | CANCELLED | BK24017 | S07 | Rahul Verma |
| 7 | 2026-10-11T09:00:00+05:30 | BOOKED | BK43146 | S14 | Sneha Kapoor |
| 8 | 2026-10-11T09:00:00+05:30 | BOOK_REJECTED_SLOT_TAKEN |  | S14 | Vikram Singh |
| 9 | 2026-10-11T09:00:00+05:30 | BOOKED | BK48757 | S11 | Vikram Singh |
