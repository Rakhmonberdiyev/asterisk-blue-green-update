import asyncio
import io, os
import time
import grpc
import numpy as np
import tts.text_processor_pb2 as text_processor_pb2, tts.text_processor_pb2_grpc as text_processor_pb2_grpc

import soundfile as sf
import tritonclient.grpc as grpcclient

from dotenv import load_dotenv

load_dotenv()

TTS_TRITON_URL = os.getenv("TTS_TRITON", "172.28.23.100:1001")
TTS_AUDIO_SAMPLE_RATE = 44100
TEXT_PROCESSOR_URL = os.getenv("TTS_TRITON", "172.28.23.100:1001")


# --- Triton klienti modul darajasida BIR MARTA yaratiladi ---
# tritonclient.grpc klienti thread-safe, shuning uchun uni
# bir nechta to_thread chaqiruvidan parallel ishlatish xavfsiz.
_triton_client = grpcclient.InferenceServerClient(url=TTS_TRITON_URL, verbose=False)


def split_text(text: str):
    def token_generator(text):
        yield text_processor_pb2.TextRequest(token=text)

    with grpc.insecure_channel(TEXT_PROCESSOR_URL) as channel:
        stub = text_processor_pb2_grpc.TextProcessingServiceStub(channel)
        responses = stub.ProcessStream(token_generator(text))
        for response in responses:
            chunk = response.processed_text.strip()
            if chunk:
                yield chunk


def infer_tts(text: str, emotion: str = "happy", sample_rate: int = 16000):
    """Bloklovchi Triton chaqiruvi. Faqat alohida threadda chaqirilishi kerak."""
    # 1. Target text input
    text_data = np.array([[text.encode('utf-8')]], dtype=object)
    inputs = [grpcclient.InferInput("target_text", [1, 1], "BYTES")]
    inputs[0].set_data_from_numpy(text_data)

    # 2. Emotion input
    emotion_data = np.array([[emotion.encode('utf-8')]], dtype=object)
    emotion_input = grpcclient.InferInput("emotion", [1, 1], "BYTES")
    emotion_input.set_data_from_numpy(emotion_data)
    inputs.append(emotion_input)

    # 3. Sample rate input
    sample_rate_data = np.array([[sample_rate]], dtype=np.int32)
    sample_rate_input = grpcclient.InferInput("sample_rate", [1, 1], "INT32")
    sample_rate_input.set_data_from_numpy(sample_rate_data)
    inputs.append(sample_rate_input)

    outputs = [grpcclient.InferRequestedOutput("waveform")]

    response = _triton_client.infer(
        model_name="f5_tts",
        inputs=inputs,
        outputs=outputs,
    )

    return response.as_numpy("waveform")[0]


def _sync_synthesize(text: str) -> bytes:
    """Bloklovchi qism — infer_tts + sf.write. Alohida threadda ishlaydi."""
    audio = infer_tts(text=text)
    buffer = io.BytesIO()
    sf.write(buffer, audio, 16000, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


i = 0


async def synthesize_tts(text):
    """Async wrapper: bloklovchi TTS ishini threadga chiqaradi,
    shunda event loop bloklanmaydi."""
    global i
    i += 1
    return await asyncio.to_thread(_sync_synthesize, text)


async def main():
    text = ("Assalomu alaykum! Xalq Bankining eyaay yordamchisiga xush kelibsiz. "
            "Men sizga pensiya, bola puli yoki ijtimoiy yordam kabi to'lovlarni "
            "qachon olishingiz haqida ma'lumot berishim mumkin. Sizga, aynan qaysi "
            "to'lov haqida ma'lumot kerak?")

    result1 = await synthesize_tts(text)

    print("Audio processed, bytes length:", len(result1))


if __name__ == "__main__":
    asyncio.run(main())