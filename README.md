# 💛 Saathi: a daily companion for elders, in their own language

Saathi phones elderly parents every day, checks whether they took their medicines, listens for health red flags, and keeps their family informed. It talks the way they talk: in any Indian language, switching mid-call if they do.

It's built entirely on **Sarvam AI**.

> India has more than 150 million people over 60, and many live apart from their children. Missed medicines and unreported symptoms (dizziness, a fall, running out of tablets) are common. A WhatsApp text doesn't help a 75-year-old who only speaks Tamil. A phone call in her own language does.

## What it does

**Senior's phone (`/senior`)**
- At medicine time, the screen rings like a real incoming call.
- The call is hands-free: the senior just talks. Saathi detects when they stop, replies out loud, and can be interrupted.
- Languages are auto-detected. Hindi, Tamil, Kannada and others all work, and the senior can switch mid-call. Saathi replies in the same language and script.
- Saathi is warm and respectful ("aap", "ji"). It never scolds and never gives medical advice.

**Family dashboard (`/family`)**
- 📷 **Prescription to schedule:** upload a photo of the prescription. Sarvam Vision extracts each medicine, its strength, and its 1-0-1 timing.
- 💊 **Today's doses:** taken, missed or pending, with the reason for a miss ("forgot", "ran out").
- 🚨 **Alerts:** 🟡 for a missed dose, 🔴 for red flags (dizziness, chest pain, a fall, confusion, ran out of medicine), each with the senior's exact words.
- 📞 **Call log:** the full transcript in the original language with an English translation, plus an AI summary and mood.

`/` shows both screens side by side for demos.

## How it uses Sarvam AI

| Step | Sarvam API | Model |
|---|---|---|
| Senior speaks, then transcript and detected language | Speech-to-Text | `saaras:v4` (auto language detection, with medicine names as hint words) |
| Reply, dose updates and alerts as JSON in one call | Chat Completions | `sarvam-105b` |
| Saathi speaks the reply | Text-to-Speech | `bulbul:v3` |
| Prescription photo to structured medicine list | Document AI (Extract) | Sarvam Vision |
| English translations and call summary for the family | Chat Completions | `sarvam-105b` |

```
 mic (16 kHz WAV, voice-activity detected) ─▶ Saaras STT ─▶ Sarvam-105B ─▶ Bulbul TTS ─▶ speaker
                                                                 │
                                                                 ├─▶ dose log + alerts ─▶ family dashboard
                                                                 └─▶ (background) translation + summary
```

Each turn takes about 4–5 seconds end to end.

## Safety design
- Saathi never diagnoses, never suggests extra doses or changes, and never tells anyone to stop a medicine.
- Red-flag symptoms always produce a **red** alert. A keyword safety net catches cases where the model under-rates one.
- Any missed dose reaches the family, even if the model forgets to raise an alert.
- Summaries report only what was said. The prompt forbids guessing causes.
- If speech recognition isn't confident about the language (common for short replies like "haan"), it re-transcribes in the call's language.

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
echo "SARVAM_API_KEY=your_key_here" > .env
.venv/bin/uvicorn main:app --port 8000
```

Open http://localhost:8000, then:
1. On the phone screen, tap **Start** to allow sound and the mic.
2. On the dashboard, upload `sample_prescription.png` and press **Save schedule**.
3. Press **📞 Trigger check-in**, answer on the phone, and talk. For example: *"सुबह वाली गोली ले ली, रात वाली भूल गई, थोड़ा चक्कर आ रहा है"* ("I took the morning pill, forgot the night one, I'm feeling a bit dizzy").
4. Watch the red alert, the dose log and the translated transcript appear on the dashboard.

Use headphones so Saathi's voice doesn't trigger the mic. Browsers only allow the microphone on `localhost` or HTTPS.

## Project layout
```
main.py              FastAPI server: call flow, prompts, dose/alert logic, scheduler
sarvam.py            Sarvam API clients (STT, chat, TTS, Document AI)
static/senior.html   Phone-call UI with in-browser voice activity detection and WAV encoding
static/family.html   Family dashboard
static/index.html    Side-by-side demo view
sample_prescription.png
```

## Roadmap
- **Real phone calls** over telephony (Exotel or Twilio) for seniors with feature phones. The call loop is already turn-based audio, so only the transport changes.
- WhatsApp alerts and a daily digest for family.
- Streaming STT and TTS for faster, more natural turn-taking.
- Scam-call warnings, appointment reminders, and refill alerts before medicines run out.
- Weekly trends for adherence, mood and symptoms, shared with the doctor.
