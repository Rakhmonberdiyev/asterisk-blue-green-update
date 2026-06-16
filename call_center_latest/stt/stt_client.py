# stt_client.py
import time, io, re, os, asyncio
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

base_url = os.getenv("STT_API")

def decode(match):
    ijuft = {
        'Ğ': "G'", 'ğ': "g'", 'Õ': "O'", 'õ': "o'",
        'Ş': "Sh", 'ş': "sh", 'Ç': "Ch", 'ç': "ch",
    }
    return ijuft[match.group()]

async def transcribe_audio(audio_bytes: bytes):
    """Run transcription safely in a background thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_transcribe, audio_bytes)

def _sync_transcribe(audio_bytes: bytes):
    
    client = OpenAI(base_url=base_url, api_key="EMPTY")
    model = client.models.list().data[0].id
    # model = '/DATA/models/largev6uz'
    
    
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = "audio.mp3"

    start_time = time.time()
    # no need to stream here, just get the text
    transcript = client.audio.transcriptions.create(
        model=model,
        file=audio_file,
        language='uz',
        response_format="json"
    )
    transcript = transcript.text
    full_text = re.sub(r"(Ğ|ğ|Õ|õ|Ş|ş|Ç|ç)", decode, transcript)
    print(f"[STT] took {time.time() - start_time:.2f}s")
    
    if full_text[-1] != ".":
        full_text += "."
    return full_text
