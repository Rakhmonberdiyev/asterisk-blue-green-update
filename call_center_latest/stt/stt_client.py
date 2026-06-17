# stt_client.py
import time, re, os
import httpx
from dotenv import load_dotenv

load_dotenv()

STT_API = os.getenv("STT_API")
ACCESS_TOKEN = os.getenv("XAZNA_ACCESS_TOKEN")

def decode(match):
    ijuft = {
        'Ğ': "G'", 'ğ': "g'", 'Õ': "O'", 'õ': "o'",
        'Ş': "Sh", 'ş': "sh", 'Ç': "Ch", 'ç': "ch",
    }
    return ijuft[match.group()]

async def transcribe_audio(audio_bytes: bytes) -> str:
    start_time = time.time()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            STT_API,
            headers={"X-Access-Token": ACCESS_TOKEN},
            files={"audio": ("audio.wav", audio_bytes, "audio/wav")},
            data={"mdl": "Base"},
        )
        resp.raise_for_status()
        result = resp.json()
        print(f"[STT] raw response: {result}", flush=True)
        transcript = result["transcript"]

    full_text = re.sub(r"(Ğ|ğ|Õ|õ|Ş|ş|Ç|ç)", decode, transcript)
    print(f"[STT] took {time.time() - start_time:.2f}s")

    if full_text and full_text[-1] != ".":
        full_text += "."
    return full_text
