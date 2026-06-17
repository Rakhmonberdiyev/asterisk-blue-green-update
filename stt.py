import os

import requests
from dotenv import load_dotenv

load_dotenv("call_center_latest/.env")

STT_API = os.getenv("STT_API")
ACCESS_TOKEN = os.getenv("XAZNA_ACCESS_TOKEN")

audio_path = "call_center_latest/intro.wav"

with open(audio_path, "rb") as f:
    response = requests.post(
        STT_API,
        headers={"X-Access-Token": ACCESS_TOKEN},
        files={"audio": (os.path.basename(audio_path), f, "audio/wav")},
        data={"mdl": "Base"},
    )

response.raise_for_status()
print("Response:", response.json())
