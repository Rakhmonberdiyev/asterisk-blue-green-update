import asyncio
import audioop
import io
import os
import wave
import httpx

from dotenv import load_dotenv

load_dotenv()

TTS_API = os.getenv("TTS_API")
ACCESS_TOKEN = os.getenv("XAZNA_TTS_ACCESS_TOKEN")

# Asterisk's external media channel is slin16 (16-bit PCM @ 16kHz), but the
# TTS API returns audio at its own native rate (e.g. 44.1kHz) — resample so
# downstream playback code's 16kHz assumption holds.
PLAYBACK_SAMPLE_RATE = 16000


def _resample_wav_to_16k(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes), 'rb') as src:
        channels = src.getnchannels()
        sample_rate = src.getframerate()
        sampwidth = src.getsampwidth()
        frames = src.readframes(src.getnframes())

    if channels != 1:
        frames = audioop.tomono(frames, sampwidth, 0.5, 0.5)

    if sample_rate != PLAYBACK_SAMPLE_RATE:
        frames, _ = audioop.ratecv(frames, sampwidth, 1, sample_rate, PLAYBACK_SAMPLE_RATE, None)

    buf = io.BytesIO()
    with wave.open(buf, 'wb') as dst:
        dst.setnchannels(1)
        dst.setsampwidth(sampwidth)
        dst.setframerate(PLAYBACK_SAMPLE_RATE)
        dst.writeframes(frames)
    return buf.getvalue()


async def synthesize_tts(text: str, emotion: str = "Neural", format: str = "wav") -> bytes:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            TTS_API,
            headers={"X-Access-Token": ACCESS_TOKEN, "Content-Type": "application/json"},
            json={"text": text, "mdl": "Base", "format": format, "emotion": emotion},
        )
        resp.raise_for_status()
        if format == "wav":
            return _resample_wav_to_16k(resp.content)
        return resp.content


async def main():
    text = ("Assalomu alaykum! Xalq Bankining eyaay yordamchisiga xush kelibsiz. "
            "Men sizga pensiya, bola puli yoki ijtimoiy yordam kabi to'lovlarni "
            "qachon olishingiz haqida ma'lumot berishim mumkin. Sizga, aynan qaysi "
            "to'lov haqida ma'lumot kerak?")

    result1 = await synthesize_tts(text)

    print("Audio processed, bytes length:", len(result1))


if __name__ == "__main__":
    asyncio.run(main())