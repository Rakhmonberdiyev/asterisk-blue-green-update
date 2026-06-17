import asyncio
import logging
import re
import wave
import traceback
from datetime import datetime
import numpy as np
import os
from collections import deque
from dotenv import load_dotenv
from websockets.asyncio.client import connect
import time

# DB va Custom modullar
from db.queries import create_session, get_or_create_user, update_session_recordings, create_conversation
from handover import trigger_handover, r
from vad.rn_client import VoiceActivityDetector

from llm.llm_social_payment import message_streaming
from stt.stt_client import transcribe_audio
from tts.tts_grpc2 import synthesize_tts
from utils.utils import SYSTEM_PROMPT_SOCIAL, gender_detection, to_wav_bytes

from s3_storage import storage


load_dotenv()

SENTENCE_END_RE = re.compile(r'[^.!?]*[.!?]+')

# --- VAD sezgirligi (env orqali, rebuild qilmasdan sozlash mumkin) ---
# VAD_THRESHOLD: har bir audio bo'lakni "ovoz" deb hisoblash chegarasi.
#   Baland = kam sezgir (shovqinga kamroq aldanadi). Oraliq 0.0–1.0.
VAD_THRESHOLD = float(os.getenv("VAD_THRESHOLD", "0.95"))
# VAD_VOICE_MIN: 20 bo'laklik oynada yozishni boshlash uchun zarur
#   "ovoz" bo'laklari soni. Baland = kam sezgir (qisqa shovqinlarni e'tiborsiz).
VAD_VOICE_MIN = int(os.getenv("VAD_VOICE_MIN", "14"))

SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK_SIZE = int(SAMPLE_RATE * 2 * (CHUNK_MS / 1000.0))


async def tts_playback_worker(tts_queue, websocket, session, state={'status': 'speaking'}):
    print("🎧 Playback: Strict Mode", flush=True)    
    tts_buffer = []
    is_first_20_chunk = 0
    try:
        while True:
            audio_data = await tts_queue.get()
            if audio_data is None: 
                break

            if isinstance(audio_data, bytes):
                raw_pcm = audio_data[44:]
                tts_buffer.append(raw_pcm)
            
                state['status'] = "speaking"

                for audio in tts_buffer:
                    chunks = [audio[i:i + CHUNK_SIZE] for i in range(0, len(audio), CHUNK_SIZE)]
                    for chunk in chunks:                        
                        if session.new_voice_detected.is_set():
                            state['status'] = "listening"
                            return
                        await websocket.send(chunk)
                        if is_first_20_chunk >= 7:
                            await asyncio.sleep(0.019)
                        else:
                            is_first_20_chunk += 1

                is_first_20_chunk = 7
                tts_buffer.clear()
                state['status'] = "listening"

            tts_queue.task_done()

    except Exception as e:
        print(f"Playback error: {e}")


async def process_segment(audio_bytes, conversation, tts_queue, resume_callback,
                           websocket, db_session_id: int, turn_index: list,
                           session_pcm_chunks: list, session_id: str = None):
    turn = turn_index[0]
    turn_index[0] += 1
    base_folder = f"recordings/session_{db_session_id}"

    try:
        
        filename=f"turn{turn:03d}_user.wav"
        user_wav_path = os.path.join(base_folder, filename)

        session_pcm_chunks.append(bytes(audio_bytes))

        wav_buffer = to_wav_bytes(bytes(audio_bytes))
        
        await storage.put_object(user_wav_path, wav_buffer.getvalue())
        
        
        text = await transcribe_audio(wav_buffer.getvalue())
        if not text or not text.strip():
            resume_callback()
            return

        print(f"📝 User: {text}")
        conversation.append({"role": "user", "content": text})

        await asyncio.to_thread(
            create_conversation, role="user", message=text, speech_path=user_wav_path, session_id=db_session_id
        )

        ai_audio_chunks = []
        full_ai_text = []
        text_buffer = ""

        async def speak(txt):
            txt = txt.strip()
            if not txt:
                return
            print(txt)
            audio = await synthesize_tts(txt)
            full_ai_text.append(txt)
            raw_pcm = audio[44:] if len(audio) > 44 else audio
            ai_audio_chunks.append(raw_pcm)
            await tts_queue.put(audio)

        async for tok in message_streaming(conversation, "uz", websocket, session_id, db_session_id):
            if tts_queue is None:
                break
            text_buffer += tok

            while True:
                match = SENTENCE_END_RE.search(text_buffer)
                if not match:
                    break
                await speak(match.group())
                text_buffer = text_buffer[match.end():]

        if tts_queue is not None:
            await speak(text_buffer)

        ai_text = " ".join(full_ai_text)
        ai_pcm = b"".join(ai_audio_chunks)

        print("\n\nFull ai text:", ai_text, "\n\n")
        
        filename=f"turn{turn:03d}_ai.wav"
        ai_wav_path = os.path.join(base_folder, filename)

        wav_buffer = to_wav_bytes(bytes(ai_pcm))
        await storage.put_object(ai_wav_path, wav_buffer.getvalue())
        
        session_pcm_chunks.append(ai_pcm)

        conversation.append({"role": "assistant", "content": ai_text})
        await asyncio.to_thread(
            create_conversation, role="assistant", message=ai_text, speech_path=ai_wav_path, session_id=db_session_id
        )

    except Exception as e:
        print(f"Pipeline error: {e}")
        traceback.print_exc()
    finally:
        resume_callback()


class VoiceSession:
    def __init__(self):
        self.voice_window = deque(maxlen=20)
        self.silence_window = deque(maxlen=24)
        self.audio_buffer = bytearray()
        self.last_two_second_buffer = deque(maxlen=40)
        self.is_recording = False
        self.new_voice_detected = asyncio.Event()

    def push_chunk(self, chunk):
        """gRPC dan mustaqil ravishda audioni real-vaqt rejimida yig'ib borish."""
        self.last_two_second_buffer.append(chunk)
        if self.is_recording:
            self.audio_buffer.extend(chunk)

    def update_vad_status(self, is_voice, state, ws):
        """Faqatgina kelgan True/False signaliga asosan holatni tekshirish."""
        self.voice_window.append(is_voice)
        self.silence_window.append(not is_voice)

        if not self.is_recording and sum(self.voice_window) >= VAD_VOICE_MIN and (state['status'] == "listening" or state['status'] == "speaking"):
            print("Voice detected🔴", flush=True)
            self.is_recording = True
            # O'tgan 2 soniyalik pre-roll buferini yozib olish oqimiga qo'shish
            for past in self.last_two_second_buffer: 
                self.audio_buffer.extend(past)
            self.new_voice_detected.set()
            # asyncio.create_task(ws.send("FLUSH_MEDIA"))
            return False

        if self.is_recording and len(self.silence_window) >= 24 and sum(self.silence_window) >= 20:
            self.is_recording = False
            return True
        return False

    def reset(self):
        buf = self.audio_buffer
        self.audio_buffer = bytearray()
        self.new_voice_detected.clear()
        return buf


def lower_volume_np(audio_bytes: bytes, volume: float) -> bytes:
    samples = np.frombuffer(audio_bytes, dtype=np.int16).copy()
    samples = (samples * volume).clip(-32768, 32767).astype(np.int16)
    return samples.tobytes()


class AstMediaWebSocket:
    def __init__(self, tag=None, log_level=None, session_id=None, caller_num=None, incoming_channel=None, ari_client=None):
        self.session_id = session_id
        self.caller_num = caller_num
        self.incoming_channel = incoming_channel
        self.ari_client = ari_client
        
        print("AstMediaWebSocket init......", flush=True)
        self.logger = logging.getLogger(__name__)
        self.tag = tag or "call"
        self.audio_file = None
        self.file_path = None

        self.audio_buffer = bytearray()
        self.target_size = 1600  # 16000 * (50 / 1000) * 2 = 50ms chunk
        
        if log_level: 
            self.logger.setLevel(log_level)

    def log(self, level, message):
        self.logger.log(level, f"{self.tag}: {message}")

    def setup_recording(self):
        """Prepares a WAV file for 16kHz Mono 16-bit PCM (slin16)."""
        os.makedirs("recordings", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.file_path = f"recordings/recording_{self.tag}_{timestamp}.wav"
        
        try:
            self.audio_file = wave.open(self.file_path, 'wb')
            self.audio_file.setnchannels(1)      
            self.audio_file.setsampwidth(2)     
            self.audio_file.setframerate(16000) 
            self.log(logging.INFO, f"File recording started: {self.file_path}")
        except Exception as e:
            self.log(logging.ERROR, f"Failed to setup recording: {e}")

    async def process_media(self, ws_media):
        self.log(logging.INFO, "Media WebSocket stream active.")
        self.setup_recording()

        db_session_id = None
        try:
            user_id = await asyncio.to_thread(get_or_create_user, self.caller_num or "unknown")
            db_session_id = await asyncio.to_thread(create_session, user_id=user_id)
            self.log(logging.INFO, f"DB Session created: {db_session_id} for user: {user_id}")
        except Exception as e:
            self.log(logging.ERROR, f"DB init error: {e}")

        turn_index = [0]
        session_pcm_chunks = []

        session = VoiceSession()
        tts_queue = asyncio.Queue(maxsize=50)     

        state = {'status': 'listening'}
        greeting_text = "Assalomu alaykum, Xalq Bankining Eaay yordamchisi. Sizga qanday yordam bera olaman."
        conversation = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT_SOCIAL
            },
            {
                "role": "assistant",
                "content": greeting_text
            }
        ]

        playback_task = asyncio.create_task(tts_playback_worker(tts_queue, ws_media, session, state))

        greeting_audio = await synthesize_tts(greeting_text)
        await tts_queue.put(greeting_audio)

        def resume(): 
            nonlocal state
            state["status"] = "listening"
        
        local_vad = VoiceActivityDetector(threshold=VAD_THRESHOLD)

        async def receive_ws_audio():
            """Asteriskdan audio oqimini real-time qabul qiluvchi va lokal VAD orqali tekshiruvchi."""
            nonlocal playback_task
            try:
                async for msg in ws_media:
                    if isinstance(msg, bytes):
                        self.audio_buffer.extend(msg)

                        while len(self.audio_buffer) >= self.target_size:
                            raw_chunk = self.audio_buffer[:self.target_size]
                            self.audio_buffer = self.audio_buffer[self.target_size:]

                            chunk_bytes = bytes(raw_chunk)

                            if self.audio_file:
                                await asyncio.to_thread(self.audio_file.writeframes, chunk_bytes)

                            # 🟢 REAL-TIME BUFFERING: Ovozni darhol deque'ga saqlaymiz. Tarmoq kechikishi ta'sir qilmaydi!
                            session.push_chunk(chunk_bytes)

                            # CPU operatsiyalarini (denoise + Silero VAD) thread'ga chiqaramiz
                            low_vol_chunk = await asyncio.to_thread(lower_volume_np, chunk_bytes, 0.5)
                            is_voice = await asyncio.to_thread(local_vad.process_frame, low_vol_chunk)

                            if session.update_vad_status(is_voice, state, ws=ws_media):
                                state['status'] = "processing"

                                session.new_voice_detected.set()
                                if playback_task:
                                    playback_task.cancel()

                                while not tts_queue.empty():
                                    tts_queue.get_nowait()

                                session.new_voice_detected.clear()
                                playback_task = asyncio.create_task(tts_playback_worker(tts_queue, ws_media, session, state))

                                # Fondagi yig'ilgan to'liq va uzilishlarsiz audioni qayta ishlashga jo'natamiz
                                asyncio.create_task(process_segment(
                                    session.reset(), conversation, tts_queue, resume, ws_media,
                                    db_session_id=db_session_id, turn_index=turn_index,
                                    session_pcm_chunks=session_pcm_chunks,
                                    session_id=self.session_id
                                ))

                    elif isinstance(msg, str) and "MEDIA_STOP" in msg:
                        return
            except Exception as e:
                self.log(logging.ERROR, f"WS Receive Task Error: {e}")
                traceback.print_exc()

        try:
            await receive_ws_audio()
        finally:
            if playback_task:
                playback_task.cancel()
            if self.audio_file:
                await asyncio.to_thread(self.audio_file.close)
            self.log(logging.INFO, "Media stream closed.")

            if db_session_id:
                try:
                    mixmonitor_path = f"{self.caller_num}-{self.session_id}.wav"
                    await asyncio.to_thread(
                        update_session_recordings, session_id=db_session_id, combined_path=mixmonitor_path
                    )
                    self.log(logging.INFO, f"MixMonitor audio path saved: {mixmonitor_path}")
                except Exception as e:
                    self.log(logging.ERROR, f"MixMonitor path save error: {e}")


class AstMediaWebSocketClient(AstMediaWebSocket):
    def __init__(self, host, port, connection_id, tag=None, log_level=None,
                 session_id=None, caller_num=None, on_connected=None):
        super().__init__(tag, log_level, session_id=session_id, caller_num=caller_num)
        self.host = host
        self.port = port
        self.connection_id = connection_id
        self.session_id = session_id
        self.on_connected = on_connected

    async def connect(self):
        uri = f"ws://{self.host}:{self.port}/media/{self.connection_id}"
        print(f"DEBUG session_id: '{self.session_id}'", flush=True)

        mod_raw = r.get("operator_mod")
        mod = True if mod_raw is None else mod_raw.decode("utf-8").lower() in ("true", "1", "yes")

        if mod:
            success = await trigger_handover(self.session_id, "70f01ca8-35dd-44b0-9ef8-d69aff38866c")
            print("-"*50, flush=True)
            print(f"Handover result: {success}", flush=True)
            print("-"*50, flush=True)
        else:
            try:
                async with connect(uri, subprotocols=["media"]) as ws_media:
                    if self.on_connected:
                        await self.on_connected()
                    await self.process_media(ws_media)
            except Exception as e:
                self.log(logging.ERROR, f"Could not connect to Media WebSocket: {e}")