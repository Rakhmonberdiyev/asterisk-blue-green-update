import aiohttp
import logging, os
import requests
import redis

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SIP_SERVICE_URL = os.getenv("SIP_SERVICE_URL")

r = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    db=int(os.getenv("REDIS_DB", "0")),
)

async def trigger_handover(session_id: str, skill_group_id: str = None):
    """
    Operatorga yo'naltirish uchun handover API ni chaqiradi
    """
    url = f"{SIP_SERVICE_URL}/api/sessions/{session_id}/handover"

    body = {}
    if skill_group_id:
        body["handover_skill_group_id"] = skill_group_id

    headers = {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type": "application/json"
    }

    print("nima gap", session_id, skill_group_id)

    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(url, json=body, headers=headers) as resp:
                result = await resp.json()
                logger.info(f"Handover triggered: {resp.status} → {result}")
                return resp.status == 200
    except Exception as e:
        logger.error(f"Handover failed: {e}")
        return False
    


def get_access_token():

    try:

        token = r.get("external_api_token")

        if token:
            return token.decode('utf-8')

        url = os.getenv("SIP_SERVICE_TOKEN_URL", "")

        payload = {
            "grant_type": "client_credentials",
            "client_id": os.getenv("SIP_CLIENT_ID", ""),
            "client_secret": os.getenv("SIP_CLIENT_SECRET", "")
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }

        response = requests.post(url, data=payload, headers=headers, verify=False)

        print(response.status_code)

        # Convert JSON response to dict
        data = response.json()

        # Extract access token
        access_token = data.get("access_token")
            
        r.setex("external_api_token", 6*3600, access_token)
        
        return access_token
    
    except Exception as e:
        print("get access token error: ", e)
        return ""