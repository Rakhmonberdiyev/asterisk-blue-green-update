import asyncio
from collections import deque
import numpy as np
from silero_vad import load_silero_vad
from pyrnnoise import RNNoise
import torch



CHANNELS = 1

NUM_SAMPLES = 512
SAMPLE_RATE = 16000

# NUM_SAMPLES = 256
# SAMPLE_RATE = 8000


class VoiceActivityDetector:

    def __init__(self, threshold: float = 0.4):
        self.threshold = threshold
        self.model = load_silero_vad(onnx=True)
        self.denoiser = RNNoise(sample_rate=SAMPLE_RATE)
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
        return buf
        
    def _denoise(self, audio_int16: np.ndarray) -> np.ndarray:
        denoised = audio_int16
        for speech_probs, denoised_frame in self.denoiser.denoise_chunk(audio_int16[np.newaxis, :]):
            flat = denoised_frame.flatten().astype(np.int16)
            print(speech_probs, len(flat), end="\r")
            if len(flat) < NUM_SAMPLES:
                flat = np.pad(flat, (0, NUM_SAMPLES - len(flat)))
            denoised = flat[:NUM_SAMPLES]
        return denoised

    def _int2float(self, sound: np.ndarray) -> np.ndarray:
        sound = sound.astype('float32')
        abs_max = np.abs(sound).max()
        if abs_max > 0:
            sound *= 1 / 32768
        return sound.squeeze()


    def process_frame(self, audio_chunk: bytes):
        audio_int16 = np.frombuffer(audio_chunk, np.int16)
        denoised = self._denoise(audio_int16)
        audio_float32 = self._int2float(denoised)

        confidence = self.model(torch.from_numpy(audio_float32), SAMPLE_RATE).item()  # BUG 1 fix

        return confidence > self.threshold


    def flush(self):
        for _, _ in self.denoiser.denoise_chunk(
            np.zeros((1, NUM_SAMPLES), dtype=np.int16), partial=True
        ):
            pass