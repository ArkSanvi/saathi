"""Thin async wrappers around the Sarvam AI REST APIs."""
import asyncio
import json
import os
import re

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = "https://api.sarvam.ai"
API_KEY = os.environ.get("SARVAM_API_KEY", "")
HEADERS = {"api-subscription-key": API_KEY}

# Languages Bulbul can speak. STT detects more; for others we reply in text only.
TTS_LANGS = {"bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN", "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN"}
TTS_SPEAKER = os.environ.get("SAATHI_VOICE", "priya")

_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))


class SarvamError(RuntimeError):
    pass


def _check(resp: httpx.Response) -> dict:
    if resp.status_code >= 400:
        raise SarvamError(f"{resp.request.url.path} -> {resp.status_code}: {resp.text[:500]}")
    return resp.json()


async def speech_to_text(audio: bytes, filename: str = "audio.wav", content_type: str = "audio/wav",
                         language_code: str = "unknown", keyterms: list[str] | None = None) -> dict:
    """Transcribe with saaras:v4. language_code="unknown" auto-detects the language."""
    data = {"model": "saaras:v4", "mode": "transcribe", "language_code": language_code}
    if keyterms:
        data["keyterms"] = json.dumps(keyterms[:50], ensure_ascii=False)
    resp = await _client.post(
        f"{BASE}/speech-to-text",
        headers=HEADERS,
        # Sarvam rejects MIME parameters like "audio/webm; codecs=opus", so send the bare type.
        files={"file": (filename, audio, content_type.split(";")[0].strip())},
        data=data,
    )
    data = _check(resp)
    return {
        "transcript": (data.get("transcript") or "").strip(),
        "language_code": data.get("language_code"),
        "language_probability": data.get("language_probability"),
    }


async def chat(messages: list[dict], json_mode: bool = False, max_tokens: int = 1024, temperature: float = 0.3) -> str:
    body = {
        "model": "sarvam-105b",
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": None,  # no thinking: we need fast spoken replies
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    resp = await _client.post(f"{BASE}/v1/chat/completions", headers=HEADERS, json=body)
    data = _check(resp)
    return (data["choices"][0]["message"].get("content") or "").strip()


def parse_json(text: str) -> dict:
    """Parse model output as JSON, tolerating code fences or stray prose."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            return json.loads(match.group(0))
        raise


async def text_to_speech(text: str, language_code: str) -> str | None:
    """Return base64 WAV, or None if Bulbul doesn't support the language."""
    if language_code not in TTS_LANGS or not text:
        return None
    resp = await _client.post(
        f"{BASE}/text-to-speech",
        headers=HEADERS,
        json={
            "text": text[:2400],
            "language_code": language_code,
            "model": "bulbul:v3",
            "speaker": TTS_SPEAKER,
            "pace": 0.9,  # a little slower for seniors
            "speech_sample_rate": 24000,
        },
    )
    data = _check(resp)
    return data["audios"][0]


PRESCRIPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "patient_name": {"type": "string", "description": "Name of the patient, empty if not written"},
        "doctor_name": {"type": "string", "description": "Name of the prescribing doctor, empty if not written"},
        "medicines": {
            "type": "array",
            "description": "Every medicine prescribed on the page",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Medicine / brand name, e.g. Tab. Amlodipine"},
                    "strength": {"type": "string", "description": "Strength or dose, e.g. 5 mg, 500 mg, 1 tablet"},
                    "morning": {"type": "boolean", "description": "Taken in the morning (e.g. first digit of a 1-0-1 pattern, OD, BD)"},
                    "afternoon": {"type": "boolean", "description": "Taken in the afternoon (e.g. middle digit of a 1-1-1 pattern, TDS)"},
                    "night": {"type": "boolean", "description": "Taken at night / bedtime (e.g. last digit of a 1-0-1 pattern, HS, BD)"},
                    "food": {"type": "string", "description": "Before food, after food, or empty if not specified"},
                    "duration": {"type": "string", "description": "How long to take it, e.g. 30 days, empty if not specified"},
                },
            },
        },
    },
}

TERMINAL = {"completed", "partially_completed", "failed", "rejected"}


async def extract_prescription(image: bytes, filename: str, content_type: str, timeout_s: int = 180) -> dict:
    """Run a Doc AI extract job on a prescription image and wait for the result."""
    resp = await _client.post(
        f"{BASE}/doc-ai/v1/job/extract",
        headers=HEADERS,
        files={"file": (filename, image, content_type)},
        data={"schema": json.dumps(PRESCRIPTION_SCHEMA), "output_format": "json"},
    )
    job = _check(resp)
    job_id = job["job_id"]

    status = job.get("status")
    waited = 0
    while status not in TERMINAL:
        if waited >= timeout_s:
            raise SarvamError(f"Doc AI job {job_id} timed out (last status: {status})")
        await asyncio.sleep(3)
        waited += 3
        st = _check(await _client.get(f"{BASE}/doc-ai/v1/job/{job_id}/status", headers=HEADERS))
        status = st.get("status") or st.get("job_state")
    if status in {"failed", "rejected"}:
        raise SarvamError(f"Doc AI job {job_id} {status}: {json.dumps(st)[:500]}")

    results = _check(await _client.get(f"{BASE}/doc-ai/v1/job/{job_id}/results", headers=HEADERS))
    return {"job_id": job_id, "raw": results, "data": _find_prescription(results)}


def _find_prescription(obj):
    """Results nesting isn't fully documented; find the dict that holds `medicines`."""
    if isinstance(obj, dict):
        if isinstance(obj.get("medicines"), list):
            return obj
        for v in obj.values():
            found = _find_prescription(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_prescription(v)
            if found:
                return found
    elif isinstance(obj, str) and "medicines" in obj:
        try:
            return _find_prescription(json.loads(obj))
        except json.JSONDecodeError:
            return None
    return None
