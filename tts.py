import requests

url = "https://ai.xazna.uz/api/v1/tts/token/generate"
headers = {
    "Content-Type": "application/json",
    "X-Access-Token": "jS54Dyo6wWIWWREJTzw8VEDvtPxipiUk4HRNrldiOdI"
}
payload = {
    "text": "Salom! Bu namuna matn. Meni ismim Raximberdi",
    "mdl": "Base",
    "format": "mp3",
    "emotion": "Neural"
}

response = requests.post(url, json=payload, headers=headers)
with open("output.mp3", "wb") as f:
    f.write(response.content)