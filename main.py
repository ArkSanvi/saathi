"""Saathi — an elder companion that checks in on medicines, in any Indian language."""
import asyncio
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import sarvam

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
STATE_FILE = DATA / "state.json"

SLOT_TIMES = {"morning": "08:00", "afternoon": "14:00", "night": "20:00"}
LANG_NAMES = {
    "hi-IN": "Hindi", "en-IN": "English", "bn-IN": "Bengali", "gu-IN": "Gujarati", "kn-IN": "Kannada",
    "ml-IN": "Malayalam", "mr-IN": "Marathi", "od-IN": "Odia", "or-IN": "Odia", "pa-IN": "Punjabi",
    "ta-IN": "Tamil", "te-IN": "Telugu", "as-IN": "Assamese", "ur-IN": "Urdu", "ne-IN": "Nepali",
}

DEFAULT_MEDICINES = [
    {"id": "m1", "name": "Amlodipine", "strength": "5 mg", "purpose": "BP", "times": ["08:00"], "food": "after food"},
    {"id": "m2", "name": "Metformin", "strength": "500 mg", "purpose": "sugar", "times": ["08:00", "20:00"], "food": "after food"},
    {"id": "m3", "name": "Atorvastatin", "strength": "10 mg", "purpose": "cholesterol", "times": ["20:00"], "food": ""},
]


def now() -> datetime:
    return datetime.now()


def fresh_state() -> dict:
    s = {
        "profile": {"senior_name": "Kamla ji", "family_name": "Rohan", "relation": "son", "language": "hi-IN"},
        "medicines": DEFAULT_MEDICINES,
        "doses": [],
        "dose_date": None,
        "alerts": [],
        "calls": [],
        "ring": None,
        "rung_doses": [],
        "prescription": None,
        "messages": [],
    }
    rebuild_doses(s)
    return s


def rebuild_doses(s: dict, keep_status: bool = True) -> None:
    """Expand the medicine schedule into today's individual doses."""
    old = {d["id"]: d for d in s.get("doses", [])} if keep_status else {}
    doses = []
    for m in s["medicines"]:
        for t in m["times"]:
            did = f"{m['id']}@{t}"
            prev = old.get(did, {})
            doses.append({
                "id": did, "med_id": m["id"], "name": m["name"], "strength": m.get("strength", ""),
                "purpose": m.get("purpose", ""), "food": m.get("food", ""), "time": t,
                "status": prev.get("status", "pending"), "reason": prev.get("reason", ""),
                "updated_at": prev.get("updated_at"),
            })
    s["doses"] = sorted(doses, key=lambda d: d["time"])
    s["dose_date"] = now().date().isoformat()


def load() -> dict:
    if STATE_FILE.exists():
        s = json.loads(STATE_FILE.read_text())
    else:
        s = fresh_state()
    if s.get("dose_date") != now().date().isoformat():  # new day: reset dose log
        rebuild_doses(s, keep_status=False)
        s["rung_doses"] = []
    return s


def save(s: dict) -> None:
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2))


state = load()
lock = asyncio.Lock()


def norm_lang(code: str | None, fallback: str = "hi-IN") -> str:
    if not code:
        return fallback
    code = code.strip()
    if "-" not in code:
        code = f"{code.lower()}-IN"
    return "od-IN" if code == "or-IN" else code


def add_alert(level: str, message: str, quote: str = "", call_id: str | None = None) -> None:
    state["alerts"].insert(0, {
        "id": uuid.uuid4().hex[:8], "level": level, "message": message, "quote": quote,
        "call_id": call_id, "ts": now().isoformat(timespec="seconds"), "ack": False,
    })


def due_doses() -> list[dict]:
    hhmm = now().strftime("%H:%M")
    pending = [d for d in state["doses"] if d["status"] == "pending"]
    due = [d for d in pending if d["time"] <= hhmm]
    return due or pending


# --------------------------------------------------------------------------- prompts

SYSTEM_PROMPT = """You are "Saathi", a warm, caring companion who phones an elderly person in India every day, like a loving grandchild. You speak with {senior_name}. Their {relation} {family_name} set you up and reads your reports.

Always call yourself "Saathi" (साथी) — never any other name.

YOUR JOB ON THIS CALL
- If they mention ANY symptom or problem, respond to that FIRST (with care, and an alert) before asking about medicines.
- Check whether they took their medicines (list below), one medicine-time at a time. Use simple names ("BP wali goli", "sugar ki dawai") rather than chemical names.
- If they took it: praise them warmly.
- If they missed / forgot it: be gentle, never scold. Ask WHY (forgot? side effects? ran out? felt unwell?). If there is no problem, encourage them to take it now. NEVER suggest double doses, changing doses, or stopping medicine.
- Ask briefly how they feel (sleep, food, mood). Listen for red flags.
- When all medicines are covered and they seem fine, say a warm goodbye and set end_call=true.

LANGUAGE RULES (very important)
- Reply in the SAME language as the person's LATEST message, even if they switch languages mid-call. {lang_hint} If they mix Hindi and English, reply in natural Hindi with common English words.
- Write in the NATIVE SCRIPT of that language (Devanagari for Hindi/Marathi, Tamil script for Tamil, etc.) — never romanised — because your text is read aloud by a speech engine.
- You are female: use feminine verb forms (e.g. "मैं बता देती हूँ").
- Keep each reply to 1-2 short, simple sentences. Ask only ONE question at a time. Use respectful forms ("aap", "ji").

SAFETY
- You are not a doctor. Never diagnose or recommend medicines.
- RED FLAGS -> alert level "red" (ALWAYS red, never yellow): chest pain, breathlessness, fainting, dizziness / chakkar, a fall, confusion, slurred speech, bleeding, severe side effects, ran out of medicine, not eating for a day, very low mood or hopelessness.
- SCAMS -> alert level "red": if they mention a call/message asking for an OTP, bank or ATM PIN, Aadhaar/PAN details, KYC update, a prize, or money transfer, firmly but kindly tell them NEVER to share OTP/PIN with anyone, not even "bank officers", to hang up on such callers, and that you are telling {family_name}. Start the alert message with "Possible scam:".
- For a red flag, stay calm, tell them you are informing {family_name} right away; for chest pain, breathlessness, a fall or fainting, also tell them to call 112 or a neighbour now.
- A missed dose with no worrying reason -> alert level "yellow".

CURRENT TIME: {time}
TODAY'S DOSES (id | medicine | time | status):
{doses}

OUTPUT: write ONLY the words you say aloud — no JSON, no labels, no quotes, no stage directions."""


ANALYSIS_PROMPT = """You track an elderly person's medicines from a check-in call made by Saathi, an AI companion.
TODAY'S DOSES (id | medicine | time | status):
{doses}

Read the conversation, especially the person's LATEST message and Saathi's reply to it, and output ONLY a JSON object:
{{"dose_updates": [{{"dose_id": "<id from the list>", "status": "taken" | "missed", "reason": "short English reason if missed"}}],
  "alert": null or {{"level": "yellow" | "red", "message": "one-line English note for the family"}},
  "end_call": true or false}}

Rules:
- dose_updates: only doses the person clearly said they took or did not take. Saying they will take it now is NOT taken yet.
- alert "red" (always red): chest pain, breathlessness, fainting, dizziness/chakkar, a fall, confusion, slurred speech, bleeding,
  severe side effects, ran out of medicine, not eating for a day, hopelessness, or a possible scam (someone asking for OTP/PIN/bank/KYC
  details or money — start the message with "Possible scam:").
- alert "yellow": a missed dose with no worrying reason.
- alert null if this latest message reveals nothing new.
- end_call: true only if Saathi's reply is a goodbye that ends the call."""


def build_system_prompt(lang: str | None) -> str:
    p = state["profile"]
    if lang:
        lang_hint = f"Their latest message is in {LANG_NAMES.get(lang, lang)} ({lang}), so reply in {LANG_NAMES.get(lang, lang)}."
    else:
        lang_hint = "They typed it, so detect the language from the text itself."
    doses = "\n".join(
        f"{d['id']} | {d['name']} {d['strength']} ({d['purpose'] or 'medicine'}, {d['food'] or 'any time'}) | {d['time']} | {d['status']}"
        for d in state["doses"]
    ) or "(no medicines scheduled)"
    return SYSTEM_PROMPT.format(
        senior_name=p["senior_name"], relation=p["relation"], family_name=p["family_name"],
        lang_hint=lang_hint, time=now().strftime("%I:%M %p"), doses=doses,
    )


def find_call(call_id: str) -> dict:
    for c in state["calls"]:
        if c["id"] == call_id:
            return c
    raise HTTPException(404, "call not found")


def build_messages(call: dict, user_text: str | None, lang: str | None) -> list[dict]:
    messages = [{"role": "system", "content": build_system_prompt(lang)}]
    for t in call["turns"]:
        messages.append({"role": "assistant" if t["role"] == "saathi" else "user", "content": t["text"]})
    if user_text is None:
        due = ", ".join(f"{d['name']} ({d['time']})" for d in call["due"]) or "today's medicines"
        note = ""
        if call.get("family_messages"):
            fam = state["profile"]["family_name"]
            msgs = " / ".join(m["text"] for m in call["family_messages"])
            note = (f" First, lovingly pass on this message from {fam}, translated naturally into their language "
                    f"(say it is from {fam}): \"{msgs}\". Then")
        messages.append({"role": "user", "content": f"[The call just connected. Greet them warmly by name.{note} ask about: {due}.]"})
    else:
        content = user_text
        if lang:  # recency matters: put the language instruction right next to what they said
            name = LANG_NAMES.get(lang, lang)
            content += f"\n\n[Reply in {name}, written in {name} script.]"
        messages.append({"role": "user", "content": content})
    return messages


# Unicode blocks -> language, so each spoken sentence goes to Bulbul with the right language code.
SCRIPTS = [(0x0900, 0x097F, "hi-IN"), (0x0980, 0x09FF, "bn-IN"), (0x0A00, 0x0A7F, "pa-IN"), (0x0A80, 0x0AFF, "gu-IN"),
           (0x0B00, 0x0B7F, "od-IN"), (0x0B80, 0x0BFF, "ta-IN"), (0x0C00, 0x0C7F, "te-IN"), (0x0C80, 0x0CFF, "kn-IN"),
           (0x0D00, 0x0D7F, "ml-IN")]


def script_lang(text: str, hint: str) -> str:
    counts: dict[str, int] = {}
    for ch in text:
        o = ord(ch)
        for lo, hi, code in SCRIPTS:
            if lo <= o <= hi:
                counts[code] = counts.get(code, 0) + 1
    if not counts:
        return "en-IN" if any(c.isascii() and c.isalpha() for c in text) else hint
    best = max(counts, key=counts.get)
    if best == "hi-IN" and hint in {"mr-IN", "ne-IN"}:  # Devanagari is shared
        return hint
    return best


SENTENCE_END = re.compile(r"[।॥.!?]+[\"'”’)]*\s+")
FALLBACK_REPLY = "माफ़ कीजिए, एक बार फिर से बताइए?"


def dose_table() -> str:
    return "\n".join(
        f"{d['id']} | {d['name']} {d['strength']} ({d['purpose'] or 'medicine'}, {d['food'] or 'any time'}) | {d['time']} | {d['status']}"
        for d in state["doses"]
    ) or "(no medicines scheduled)"


async def analyse_turn(call: dict, user_text: str, reply: str) -> dict:
    """Structured side of a turn: dose updates, alert, end of call. Runs while the reply is being spoken."""
    convo = "\n".join(f"{'Saathi' if t['role'] == 'saathi' else 'Person'}: {t['text']}" for t in call["turns"][-8:])
    convo += f"\nPerson (LATEST): {user_text}\nSaathi (reply): {reply}"
    try:
        raw = await sarvam.chat([{"role": "system", "content": ANALYSIS_PROMPT.format(doses=dose_table())},
                                 {"role": "user", "content": convo}], json_mode=True, max_tokens=400, temperature=0.1)
        return sarvam.parse_json(raw)
    except Exception as e:
        print("analysis failed:", repr(e))
        return {}


async def stream_agent(call: dict, user_text: str | None, lang: str | None, out_q: asyncio.Queue) -> None:
    """Stream Saathi's reply. Puts ("sentence", text, lang) on out_q as soon as each sentence is complete,
    then ("meta", reply, reply_lang, meta). `lang` is the STT language, or None for typed input."""
    messages = build_messages(call, user_text, lang)
    hint = lang or call["language"]
    reply = ""
    for attempt in range(2):  # the model occasionally returns nothing; retry once
        reply, emitted = "", 0
        async for delta in sarvam.chat_stream(messages, max_tokens=400):
            reply += delta
            # Emit each complete sentence, merging very short ones so the speech isn't choppy.
            while (m := SENTENCE_END.search(reply, emitted)):
                chunk = reply[emitted:m.end()].strip()
                if len(chunk) < 12:
                    nxt = SENTENCE_END.search(reply, m.end())
                    if not nxt:
                        break
                    m, chunk = nxt, reply[emitted:nxt.end()].strip()
                emitted = m.end()
                await out_q.put(("sentence", chunk, script_lang(chunk, hint)))
        tail = reply[emitted:].strip()
        if tail:
            await out_q.put(("sentence", tail, script_lang(tail, hint)))
        if reply.strip():
            break
        print(f"Empty LLM reply (attempt {attempt + 1})")
    reply = reply.strip()
    if not reply:
        reply = FALLBACK_REPLY
        await out_q.put(("sentence", reply, "hi-IN"))
    reply_lang = script_lang(reply, hint)
    meta = await analyse_turn(call, user_text, reply) if user_text is not None else {}
    await out_q.put(("meta", reply, reply_lang, meta))


async def speak_stream(call: dict, user_text: str | None, lang: str | None):
    """Run the agent and TTS concurrently; yield ("audio", text, b64) in order, then ("meta", ...)."""
    q: asyncio.Queue = asyncio.Queue()
    producer = asyncio.create_task(stream_agent(call, user_text, lang, q))
    tts_tasks: list[tuple[str, asyncio.Task]] = []
    meta = None
    try:
        while meta is None:
            item = await q.get()
            if item[0] == "sentence":
                _, text, l = item
                tts_tasks.append((text, asyncio.create_task(speak(text, l))))
            else:
                meta = item
            # Yield finished audio in order without waiting for the whole reply.
            while tts_tasks and tts_tasks[0][1].done():
                text, task = tts_tasks.pop(0)
                yield ("audio", text, task.result())
        for text, task in tts_tasks:
            yield ("audio", text, await task)
        yield meta
    finally:
        await producer


RED_FLAG_WORDS = ("dizz", "chest", "breath", "faint", "fall", "fell", "confus", "slurred", "bleed",
                  "ran out", "run out", "not eaten", "hopeless", "unconscious", "vomit", "scam", "otp", "fraud")


def apply_updates(call: dict, out: dict, user_text: str) -> None:
    by_id = {d["id"]: d for d in state["doses"]}
    alerted = False
    for u in out.get("dose_updates") or []:
        d = by_id.get(u.get("dose_id"))
        if not d or u.get("status") not in {"taken", "missed"}:
            continue
        d["status"] = u["status"]
        d["reason"] = u.get("reason", "") if u["status"] == "missed" else ""
        d["updated_at"] = now().isoformat(timespec="seconds")
    alert = out.get("alert")
    if isinstance(alert, dict) and alert.get("level") in {"yellow", "red"} and alert.get("message"):
        level = alert["level"]
        # Safety net: never let a red-flag symptom be downgraded to yellow.
        if level == "yellow" and any(w in alert["message"].lower() for w in RED_FLAG_WORDS):
            level = "red"
        # The model tends to re-raise the same concern every turn; keep one alert per level per call.
        existing = next((a for a in state["alerts"] if a["call_id"] == call["id"] and a["level"] == level), None)
        if existing:
            existing["message"] = alert["message"]
            existing["ts"] = now().isoformat(timespec="seconds")
        else:
            add_alert(level, alert["message"], quote=user_text, call_id=call["id"])
        alerted = True
    # Safety net: a missed dose always reaches the family even if the model forgot to alert.
    if not alerted:
        for u in out.get("dose_updates") or []:
            d = by_id.get(u.get("dose_id"))
            if d and u.get("status") == "missed":
                add_alert("yellow", f"Missed {d['name']} ({d['time']}): {d['reason'] or 'no reason given'}",
                          quote=user_text, call_id=call["id"])


async def speak(text: str, lang: str) -> str | None:
    try:
        return await sarvam.text_to_speech(text, lang)
    except sarvam.SarvamError as e:
        print("TTS failed:", e)
        return None


# --------------------------------------------------------------------------- app

app = FastAPI(title="Saathi")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/senior")
def senior_page():
    return FileResponse(ROOT / "static" / "senior.html")


@app.get("/family")
def family_page():
    return FileResponse(ROOT / "static" / "family.html")


@app.get("/api/state")
def get_state():
    return state


@app.get("/api/ring")
def get_ring():
    return {"ring": state["ring"], "profile": state["profile"]}


@app.post("/api/trigger-call")
async def trigger_call():
    async with lock:
        due = due_doses()
        state["ring"] = {"id": uuid.uuid4().hex[:8], "at": now().isoformat(timespec="seconds"),
                         "dose_ids": [d["id"] for d in due]}
        save(state)
    return state["ring"]


def stt_keyterms() -> list[str]:
    """Words speech recognition should expect: medicine names, people, the agent's name."""
    p = state["profile"]
    terms = ["Saathi", "साथी", "BP", "sugar", "दवाई", "गोली", p["senior_name"], p["family_name"]]
    for m in state["medicines"]:
        terms.append(m["name"].replace("Tab.", "").replace("Cap.", "").strip())
    return [t for t in dict.fromkeys(terms) if t]


async def translate_turns(call: dict, turns: list[dict]) -> None:
    """Fill in English translations for the family dashboard (Sarvam Translate), off the hot path."""
    todo = [t for t in turns if t["text"] and not t.get("en")]
    if not todo:
        return

    async def one(t: dict) -> None:
        try:
            t["en"] = await sarvam.translate(t["text"], t["lang"], "en-IN")
        except Exception as e:
            print("translation failed:", e)

    await asyncio.gather(*(one(t) for t in todo))
    async with lock:
        save(state)


AUDIO_DIR = DATA / "audio"
AUDIO_DIR.mkdir(exist_ok=True)


def ndjson(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


async def respond(call: dict, user_text: str | None, lang: str | None, stt: dict | None):
    """Stream one Saathi turn to the browser, then record it and apply dose / alert updates."""
    try:
        reply, reply_lang, meta = "", call["language"], {}
        async for ev in speak_stream(call, user_text, lang):
            if ev[0] == "audio":
                yield ndjson({"type": "audio", "text": ev[1], "audio": ev[2]})
            else:
                _, reply, reply_lang, meta = ev
        async with lock:
            ts = now().isoformat(timespec="seconds")
            new_turns = []
            if user_text is not None:
                new_turns.append({"role": "senior", "text": user_text, "en": "", "lang": lang or reply_lang, "ts": ts,
                                  "via": "voice" if stt else "text", "lang_confidence": stt and stt["language_probability"]})
            new_turns.append({"role": "saathi", "text": reply, "en": "", "lang": reply_lang, "ts": ts})
            call["turns"] += new_turns
            call["language"] = reply_lang
            if user_text is not None:
                apply_updates(call, meta, user_text)
            save(state)
        asyncio.create_task(translate_turns(call, new_turns))
        yield ndjson({"type": "done", "reply": reply, "lang": reply_lang, "end_call": bool(meta.get("end_call"))})
    except Exception as e:
        print("turn failed:", repr(e))
        yield ndjson({"type": "error", "message": str(e)[:200]})


@app.post("/api/call/start")
async def start_call():
    async with lock:
        ring = state["ring"] or {"dose_ids": [d["id"] for d in due_doses()]}
        state["ring"] = None
        for c in state["calls"]:  # a new call ends any call left open (e.g. page reload)
            if c["status"] == "live":
                c["status"], c["ended"] = "ended", now().isoformat(timespec="seconds")
        call = {
            "id": uuid.uuid4().hex[:8], "started": now().isoformat(timespec="seconds"), "ended": None,
            "language": state["profile"]["language"], "turns": [], "summary": "", "status": "live",
            "due": [d for d in state["doses"] if d["id"] in ring["dose_ids"]],
            "family_messages": [m for m in state.setdefault("messages", []) if not m["delivered"]],
        }
        for m in call["family_messages"]:
            m["delivered"], m["call_id"] = True, call["id"]
        state["calls"].insert(0, call)
        save(state)

    async def gen():
        yield ndjson({"type": "call", "call_id": call["id"]})
        async for line in respond(call, None, call["language"], None):
            yield line

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/api/call/{call_id}/turn")
async def call_turn(call_id: str, audio: UploadFile | None = File(None), text: str | None = Form(None)):
    call = find_call(call_id)
    if call["status"] != "live":
        raise HTTPException(400, "call already ended")

    stt = None
    if audio is not None:
        blob = await audio.read()
        if len(blob) < 8000:  # < ~0.25s of 16 kHz audio: a click or cough
            return StreamingResponse(iter([ndjson({"type": "retry"})]), media_type="application/x-ndjson")
        fname, ctype = audio.filename or "turn.wav", audio.content_type or "audio/wav"
        (AUDIO_DIR / f"{call_id}_{len(call['turns']):02d}{Path(fname).suffix or '.wav'}").write_bytes(blob)
        keyterms = stt_keyterms()
        stt = await sarvam.speech_to_text(blob, fname, ctype, keyterms=keyterms)
        conf = stt["language_probability"] or 0
        # Short replies ("haan", "ji") are easy to mis-detect; if unsure, re-transcribe in the call's language.
        if conf < 0.6 and call["language"] in sarvam.TTS_LANGS:
            print(f"low language confidence {conf:.2f} ({stt['language_code']}): retrying as {call['language']}")
            stt = {**await sarvam.speech_to_text(blob, fname, ctype, language_code=call["language"], keyterms=keyterms),
                   "language_code": call["language"], "language_probability": conf}
        user_text = stt["transcript"]
        lang = norm_lang(stt["language_code"], call["language"])
        print(f"STT [{lang} p={conf:.2f}]: {user_text}")
    elif text and text.strip():
        user_text = text.strip()
        lang = script_lang(user_text, call["language"])  # typed: infer language from the script
    else:
        raise HTTPException(400, "send audio or text")
    if not user_text:
        return StreamingResponse(iter([ndjson({"type": "retry"})]), media_type="application/x-ndjson")

    async def gen():
        yield ndjson({"type": "heard", "text": user_text, "lang": lang})
        async for line in respond(call, user_text, lang, stt):
            yield line

    return StreamingResponse(gen(), media_type="application/x-ndjson")


SUMMARY_PROMPT = """Summarise this check-in call between Saathi (an AI companion) and an elderly person for their family.
Write 2-3 short English sentences: medicines taken/missed (and why), how they are feeling, anything the family should do.
Report only what was said. Do NOT guess causes or link symptoms to medicines, and never say a symptom is resolved unless they said so.
Then on a new line write: MOOD: <one word>."""


@app.post("/api/call/{call_id}/end")
async def end_call(call_id: str):
    call = find_call(call_id)
    if call["status"] == "ended":
        return call
    call["status"] = "ended"
    call["ended"] = now().isoformat(timespec="seconds")
    convo = "\n".join(f"{t['role']}: {t['en'] or t['text']}" for t in call["turns"])
    if len(call["turns"]) > 1:
        try:
            raw = await sarvam.chat([{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": convo}],
                                    max_tokens=300)
            summary, _, mood = raw.partition("MOOD:")
            call["summary"], call["mood"] = summary.strip(), mood.strip().strip(".")
        except sarvam.SarvamError as e:
            call["summary"] = f"(summary unavailable: {e})"
    else:
        call["summary"] = "Call ended before the conversation started."
        add_alert("yellow", "Check-in call was not completed.", call_id=call["id"])
    async with lock:
        save(state)
    return call


class Medicine(BaseModel):
    id: str | None = None
    name: str
    strength: str = ""
    purpose: str = ""
    times: list[str]
    food: str = ""


class Profile(BaseModel):
    senior_name: str
    family_name: str
    relation: str
    language: str


@app.post("/api/medicines")
async def set_medicines(meds: list[Medicine]):
    async with lock:
        state["medicines"] = [
            {**m.model_dump(), "id": m.id or f"m{uuid.uuid4().hex[:5]}", "times": sorted(set(m.times))} for m in meds
        ]
        rebuild_doses(state)
        save(state)
    return state["medicines"]


@app.post("/api/profile")
async def set_profile(p: Profile):
    async with lock:
        state["profile"] = p.model_dump()
        save(state)
    return state["profile"]


@app.post("/api/prescription")
async def upload_prescription(file: UploadFile = File(...)):
    blob = await file.read()
    ctype = file.content_type or "image/jpeg"
    if ctype not in {"image/jpeg", "image/png", "application/pdf"}:
        raise HTTPException(400, "Upload a JPG, PNG or PDF")
    try:
        res = await sarvam.extract_prescription(blob, file.filename or "prescription.jpg", ctype)
    except sarvam.SarvamError as e:
        raise HTTPException(502, str(e))
    data = res["data"] or {}
    meds = []
    for i, m in enumerate(data.get("medicines") or []):
        times = [SLOT_TIMES[slot] for slot in ("morning", "afternoon", "night") if m.get(slot)]
        meds.append({
            "id": f"rx{i}", "name": m.get("name", "").strip(), "strength": m.get("strength", ""),
            "purpose": "", "times": times or ["08:00"], "food": m.get("food") or "",
        })
    async with lock:
        state["prescription"] = {"job_id": res["job_id"], "patient": data.get("patient_name", ""),
                                 "doctor": data.get("doctor_name", ""), "at": now().isoformat(timespec="seconds")}
        save(state)
    (DATA / f"rx_{res['job_id']}.json").write_text(json.dumps(res["raw"], ensure_ascii=False, indent=2))
    return {"medicines": meds, "patient": data.get("patient_name", ""), "doctor": data.get("doctor_name", ""),
            "found": bool(res["data"])}


class FamilyMessage(BaseModel):
    text: str


@app.post("/api/message")
async def send_message(msg: FamilyMessage):
    """Family leaves a message; Saathi delivers it, in the senior's language, at the start of the next call."""
    if not msg.text.strip():
        raise HTTPException(400, "empty message")
    async with lock:
        state.setdefault("messages", []).insert(0, {
            "id": uuid.uuid4().hex[:8], "text": msg.text.strip()[:500], "ts": now().isoformat(timespec="seconds"),
            "delivered": False, "call_id": None,
        })
        save(state)
    return {"ok": True}


@app.post("/api/ack/{alert_id}")
async def ack_alert(alert_id: str):
    async with lock:
        for a in state["alerts"]:
            if a["id"] == alert_id:
                a["ack"] = True
        save(state)
    return {"ok": True}


@app.post("/api/reset")
async def reset():
    global state
    async with lock:
        state = fresh_state()
        save(state)
    return {"ok": True}


async def scheduler():
    """Ring the senior's screen when a dose comes due (once per dose)."""
    while True:
        await asyncio.sleep(20)
        async with lock:
            if state.get("dose_date") != now().date().isoformat():
                rebuild_doses(state, keep_status=False)
                state["rung_doses"] = []
            hhmm = now().strftime("%H:%M")
            newly_due = [d for d in state["doses"]
                         if d["status"] == "pending" and d["time"] <= hhmm and d["id"] not in state["rung_doses"]]
            live = any(c["status"] == "live" for c in state["calls"])
            if newly_due and not state["ring"] and not live:
                state["rung_doses"] += [d["id"] for d in newly_due]
                state["ring"] = {"id": uuid.uuid4().hex[:8], "at": now().isoformat(timespec="seconds"),
                                 "dose_ids": [d["id"] for d in due_doses()]}
                save(state)


@app.on_event("startup")
async def _start_scheduler():
    # Don't auto-ring for doses already past when the server starts; the demo uses the trigger button.
    hhmm = now().strftime("%H:%M")
    state["rung_doses"] = list({*state.get("rung_doses", []), *(d["id"] for d in state["doses"] if d["time"] <= hhmm)})
    asyncio.create_task(scheduler())
