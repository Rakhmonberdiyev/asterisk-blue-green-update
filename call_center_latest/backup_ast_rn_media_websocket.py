import asyncio
import logging
import wave
import traceback
from datetime import datetime
from websockets.asyncio.client import connect

from db.queries import create_session, get_or_create_user, update_session_recordings
import asyncio, io, wave, os, websockets, grpc, grpc.aio, tts.text_processor_pb2 as text_processor_pb2, tts.text_processor_pb2_grpc as text_processor_pb2_grpc
from collections import deque
from dotenv import load_dotenv
from llm.llm_social_payment import message_streaming, initialize_mcp
from stt.stt_client import transcribe_audio
from tts.tts_grpc2 import synthesize_tts
from utils.utils import SYSTEM_PROMPT_SOCIAL, gender_detection, merge_wav_files, save_pcm_to_wav
from vad.rn_client import VoiceActivityDetector

load_dotenv()

TEXT_PROCESSOR_URL = os.getenv("TTS_TRITON")


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
            if audio_data is None: break

            if isinstance(audio_data, bytes):
                raw_pcm = audio_data[44:]
                tts_buffer.append(raw_pcm)
            elif isinstance(audio_data, str) and audio_data == "ready":
                state['status'] = "speaking"

                for audio in tts_buffer:
                    chunks = [audio[i:i + CHUNK_SIZE] for i in range(0, len(audio), CHUNK_SIZE)]
                    
                    for chunk in chunks:
                        if session.new_voice_detected.is_set():
                            state['status'] = "listening"
                            return
            
                        await websocket.send(chunk)

                        if is_first_20_chunk >= 10:
                            await asyncio.sleep(0.019)
                        else:
                            is_first_20_chunk += 1
                        
                is_first_20_chunk = 10
                tts_buffer.clear()
                state['status'] = "listening"

            tts_queue.task_done()

    except Exception as e:
        print(f"Playback error: {e}")
    



async def process_segment(audio_bytes, conversation, tts_queue, resume_callback,
                           websocket, db_session_id: int, turn_index: list,
                           session_pcm_chunks: list, session_id: str = None):  # ✅ shared list
    from db.queries import create_conversation

    turn = turn_index[0]
    turn_index[0] += 1
    base_folder = f"recordings/session_{db_session_id}"

    try:
        # User audio saqlash
        user_wav_path = save_pcm_to_wav(
            audio_bytes, folder=base_folder,
            filename=f"turn{turn:03d}_user.wav"
        )

        count_system = sum(1 for msg in conversation if msg["role"] == "system")
        if count_system == 1:
            gender, confidence = await gender_detection(audio_bytes)
            if confidence and confidence >= 0.5:
                conversation.append({"role": "system", "content": f"User gender is {gender}. Adjust tone accordingly."})

        # ✅ User PCM ni session umumiy listiga qo'shish
        session_pcm_chunks.append(bytes(audio_bytes))

        with open(user_wav_path, 'rb') as f:
            wav_data = f.read()

        text = await transcribe_audio(wav_data)
        if not text or not text.strip():
            resume_callback()
            return

        print(f"📝 User: {text}")
        conversation.append({"role": "user", "content": text})

        create_conversation(role="user", message=text,
                            speech_path=user_wav_path, session_id=db_session_id)

        ai_audio_chunks = []
        full_ai_text = []

        async with grpc.aio.insecure_channel(TEXT_PROCESSOR_URL) as channel:
            stub = text_processor_pb2_grpc.TextProcessingServiceStub(channel)

            async def gen():
                async for tok in message_streaming(conversation, "uz", websocket, session_id):
                    yield text_processor_pb2.TextRequest(token=tok)

            async for resp in stub.ProcessStream(gen()):
                if tts_queue is None:
                    break
                txt = resp.processed_text.strip()
                if not txt:
                    continue

                audio = await synthesize_tts(txt)
                full_ai_text.append(txt)
                raw_pcm = audio[44:] if len(audio) > 44 else audio
                ai_audio_chunks.append(raw_pcm)

                await tts_queue.put(audio)

            await tts_queue.put("ready")

        ai_text = " ".join(full_ai_text)
        ai_pcm = b"".join(ai_audio_chunks)

        ai_wav_path = save_pcm_to_wav(
            ai_pcm, folder=base_folder,
            filename=f"turn{turn:03d}_ai.wav"
        )

        # ✅ AI PCM ni ham session umumiy listiga qo'shish
        session_pcm_chunks.append(ai_pcm)

        conversation.append({"role": "assistant", "content": ai_text})
        create_conversation(role="assistant", message=ai_text,
                            speech_path=ai_wav_path, session_id=db_session_id)

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
        

    def update_vad(self, chunk, is_voice, state):
        self.voice_window.append(is_voice)
        self.silence_window.append(not is_voice)
        self.last_two_second_buffer.append(chunk)

        if self.is_recording:
            self.audio_buffer.extend(chunk)

        if not self.is_recording and sum(self.voice_window) >= 12 and (state['status'] == "listening" or state['status'] == "speaking"):
            print("Voice detected🔴", flush=True)
            self.is_recording = True
            for past in self.last_two_second_buffer: self.audio_buffer.extend(past)
            self.new_voice_detected.set()
            return False

        if self.is_recording and len(self.silence_window) >= 24 and sum(self.silence_window) >= 20:
            self.is_recording = False
            return True
        return False

    def reset(self):
        buf = self.audio_buffer
        self.audio_buffer = bytearray()
        self.new_voice_detected.clear()

        self.voice_window.clear()
        self.silence_window.clear()
        self.last_two_second_buffer.clear()

        return buf





class AstMediaWebSocket:
    def __init__(self, tag=None, log_level=None, session_id=None, caller_num=None, incoming_channel=None, ari_client=None):
        
        self.session_id = session_id
        self.caller_num = caller_num
        self.incoming_channel = incoming_channel
        self.ari_client = ari_client
        
        print("AstMediaWebSocket init......",flush=True)
        self.logger = logging.getLogger(__name__)
        self.tag = tag or "call"
        self.audio_file = None
        self.file_path = None

        self.audio_buffer = bytearray()
        self.target_size = 1600 # azizjon vad 50ms -> 16000 * (50 / 1000) * 2 * 1
        
        if log_level: self.logger.setLevel(log_level)

    def log(self, level, message):
        self.logger.log(level, f"{self.tag}: {message}")

    def setup_recording(self):
        """Prepares a WAV file for 16kHz Mono 16-bit PCM (slin16)."""
        os.makedirs("recordings", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.file_path = f"recordings/recording_{self.tag}_{timestamp}.wav"
        
        try:
            self.audio_file = wave.open(self.file_path, 'wb')
            self.audio_file.setnchannels(1)      # Mono
            self.audio_file.setsampwidth(2)     # 16-bit PCM = 2 bytes
            self.audio_file.setframerate(16000) # 16kHz for slin16
            self.log(logging.INFO, f"File recording started: {self.file_path}")
        except Exception as e:
            self.log(logging.ERROR, f"Failed to setup recording: {e}")


    async def process_media(self, ws_media):

        
        self.log(logging.INFO, "Media WebSocket stream active.")
        # self.setup_recording()

        db_session_id = None
        try:
            user_id = get_or_create_user(self.caller_num or "unknown")
            db_session_id = create_session(user_id=user_id)
            self.log(logging.INFO, f"DB Session created: {db_session_id} for user: {user_id}")
        except Exception as e:
            self.log(logging.ERROR, f"DB init error: {e}")


        turn_index = [0]
        session_pcm_chunks = []

        session = VoiceActivityDetector()
        tts_queue = asyncio.Queue(maxsize=50)     

        state = {'status': 'listening'}
        conversation = [
            {                    
                "role": "system",
                "content": SYSTEM_PROMPT_SOCIAL
            },
            {
                "role": "assistant",
                "content": "Assalomu alaykum, Xalq Bankining Eaay yordamchisiga xush kelibsiz, men sizga, pensiya, bola puli, moddiy yordam kabi ijtimoiy to'lovlar qachon tushishi va baraka kartasi bo'yicha savollaringizga javob berishim mumkin. Sizga aynan qaysi xizmatimiz bo'yicha yordam kerak."
            }
        ]

        playback_task = asyncio.create_task(tts_playback_worker(tts_queue, ws_media, session, state))

        def resume(): nonlocal state; state["status"] = "listening"
        
        input_q = asyncio.Queue()

        async def vad_gen():
            try:
                async for msg in ws_media:
                    if isinstance(msg, bytes):
                        self.audio_buffer.extend(msg)
                        
                        while len(self.audio_buffer) >= self.target_size:
                            ai_chunk_raw = self.audio_buffer[:self.target_size]
                            self.audio_buffer = self.audio_buffer[self.target_size:]
                            
                            ai_chunk = bytes(ai_chunk_raw)
                            await input_q.put(ai_chunk)
                            
                            yield session.process_frame(ai_chunk)
                    
                    elif isinstance(msg, str) and "MEDIA_STOP" in msg:
                        return
            except Exception as e:
                self.log(logging.ERROR, f"Generator error: {e}")
            finally:
                session.flush()

        try:
            # This loop consumes the VAD results from the gRPC stream
            async for result in vad_gen():
                # Get the corresponding audio chunk we queued up
                chunk = await input_q.get() 
                
                # Also write original frames to file (or do it inside vad_gen)
                if self.audio_file:
                    self.audio_file.writeframes(chunk)

                if session.update_vad(chunk, result, state):
                    state['status'] = "processing"
                    
                    session.new_voice_detected.set()
                    if playback_task: playback_task.cancel()
                    
                    while not tts_queue.empty(): tts_queue.get_nowait()
                    
                    session.new_voice_detected.clear()
                    playback_task = asyncio.create_task(tts_playback_worker(tts_queue, ws_media, session, state))

                    asyncio.create_task(process_segment(session.reset(), conversation, tts_queue, resume, ws_media, db_session_id=db_session_id, turn_index=turn_index, session_pcm_chunks=session_pcm_chunks, session_id=self.session_id))
        
        except Exception as e:
            self.log(logging.ERROR, f"VAD Loop Error: {e}")
            traceback.print_exc()
        finally:
            if playback_task: playback_task.cancel()
            if self.audio_file: self.audio_file.close()
            self.log(logging.INFO, "Media stream closed.")
        
            # ✅ Session tugadi — barcha chunklar birlashtiriladi
            if db_session_id and session_pcm_chunks:
                try:
                    all_pcm = b"".join(session_pcm_chunks)
                    combined_path = save_pcm_to_wav(
                        all_pcm,
                        folder=f"recordings/session_{db_session_id}",
                        filename="full_combined.wav"
                    )
                    update_session_recordings(
                        session_id=db_session_id,
                        combined_path=combined_path
                    )
                    self.log(logging.INFO, f"Full combined saved: {combined_path}")
                except Exception as e:
                    self.log(logging.ERROR, f"Combined save error: {e}")

            self.log(logging.INFO, "Media stream closed.")

class AstMediaWebSocketClient(AstMediaWebSocket):
    def __init__(self, host, port, connection_id, tag=None, 
                 log_level=None, session_id=None, caller_num=None):
        super().__init__(tag, log_level, session_id=session_id, 
                         caller_num=caller_num)
        self.host = host
        self.port = port
        self.connection_id = connection_id
        self.session_id = session_id

    async def connect(self):
        uri = f"ws://{self.host}:{self.port}/media/{self.connection_id}"
        print(f"DEBUG session_id: '{self.session_id}'", flush=True)  # ← shu qo'shilsin
        while True:
            try:
                async with connect(uri, subprotocols=["media"]) as ws_media:
                    await self.process_media(ws_media)
            except Exception as e:
                self.log(logging.ERROR, f"Could not connect to Media WebSocket: {e}")

            await asyncio.sleep(5)