import asyncio, os
from fastmcp import Client, FastMCP
from openai import AsyncOpenAI
import pandas as pd
import re
from fastmcp.server import create_proxy
from dotenv import load_dotenv

load_dotenv()

client = AsyncOpenAI(api_key=os.getenv("LLM_API_KEY"), base_url=os.getenv("LLM_BASE_URL", "http://172.28.23.100:1001/llm/v1"))
main = FastMCP("Main")

_cached_tools: list | None = None
_df = None
_initialized = False

SKILL_GROUP_IDS = {
    "1": os.getenv("OPERATOR_SKILL_GROUP_ID_1", "5570f646-8e99-41cb-a2e3-8fec3364ddcf4"),   # Umumiy bank ma'lumotlari
    "2": os.getenv("OPERATOR_SKILL_GROUP_ID_2", "a88dd360-440e-4219-bb28-41e8582e0a74"),   # Hisob raqam ma'lumotlari
    "3": os.getenv("OPERATOR_SKILL_GROUP_ID_3", "d9ba2d0a-b0ba-41ef-b835-27c07c73e9f8"),   # Shikoyat va takliflar
    "4": os.getenv("OPERATOR_SKILL_GROUP_ID_4", "a3a2da09-7439-4c09-a404-e5cdb93e4441"),   # Hazna / masofaviy xizmatlar
    "5": os.getenv("OPERATOR_SKILL_GROUP_ID_5", "cef839e9-69bb-429d-b09e-21de669a673c"),   # Pensiya / ijtimoiy to'lovlar (IVR 5)
}


async def playback(websocket, audio_file):
    try:
        print(f"Playing IVR {audio_file} audio file...")
        # ivr_name = "ivr_" + ivr_number + "_16k.wav"
        with open(audio_file, "rb") as f:
            audio_bytes = f.read()

        # 16kHz, 16-bit mono = 32000 bytes/sec → 640 bytes = 20ms per chunk
        CHUNK_SIZE = 640
        CHUNK_DURATION = (CHUNK_SIZE / 32000) - 0.002  # 0.02 sec

        chunks = [audio_bytes[i:i + CHUNK_SIZE] for i in range(0, len(audio_bytes), CHUNK_SIZE)]
        total_duration = len(chunks) * CHUNK_DURATION
        print(f"Audio {audio_file}: {len(chunks)} chunks, ~{total_duration:.1f}s", flush=True)

        for chunk in chunks:
            await websocket.send(chunk)
            await asyncio.sleep(CHUNK_DURATION)  # ← real-time kutish

    except Exception as e:
        print(f"Error sending audio file: {e}", flush=True)

def is_server_alive(base_url):
    import requests
    try:
        r = requests.get(f"{base_url}", timeout=2)
        return r.status_code == 200
    except Exception as e:
        print(f"Error occurred while checking server health for {base_url}: {e}")
        return False


async def initialize_mcp():
    global _initialized, main
    if _initialized:
        return

    print("\n---------------------HEALTH CHECK AND MOUNTING BEGIN-----------------------------------\n")
    if is_server_alive(os.getenv("MCP_PENSION_HEALTH")):
        print("MCP_PENSION is alive.")
        main.mount(create_proxy(Client(os.getenv("MCP_PENSION"))), namespace="pension")
        # main.mount(Client(os.getenv("MCP_PENSION")), namespace="pension")

    else:
        print(f"Warning: MCP_PENSION at {os.getenv('MCP_PENSION')} is not reachable.")

    print()
    _initialized = True

    mcp_tools = await main.list_tools()
    print(len(mcp_tools), "tools loaded for LLM:")
    print("\n---------------------HEALTH CHECK AND MOUNTING END-----------------------------------\n")


async def get_tools() -> list:
    global _cached_tools
    if _cached_tools is not None:
        return _cached_tools

    async with Client(main) as c:
        mcp_tools = await c.list_tools()
        _cached_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                }
            }
            for tool in mcp_tools
        ]
    return _cached_tools


async def get_model_name():
    global client
    print("Checking LLM model availability...", flush=True)
    print("Current llm: ", client.base_url)
    try:
        models_list = await client.models.list()
        model = models_list.data[0].id
        return model
    except:
        endpoint = str(client.base_url).split("/")[-3]
        if endpoint == "llm":
            print("-"*20, "QWEN is shutting down. Trying with GEMMA...", "-"*20, flush=True)
            base_url = os.getenv("LLM_BASE_URL_GEMMA")
        else:
            print("-"*20, "GEMMA is shutting down. Trying with QWEN...", "-"*20, flush=True)
            base_url = os.getenv("LLM_BASE_URL")

        client = AsyncOpenAI(api_key=os.getenv("LLM_API_KEY"), base_url=base_url)
        try:
            models_list = await client.models.list()
            model = models_list.data[0].id
            return model
        except:
            print("Both LLM models is shutting down...", flush=True)
            return False



def validate_passport(raw: str) -> str | None:
    cleaned = re.sub(r'[\s\-_]', '', raw).upper()
    if re.fullmatch(r'[A-Z]{2}\d{7}', cleaned):
        return cleaned
    return None


def get_passport_variants(passport: str) -> list[str]:
    if len(passport) < 9:
        return [passport]
    l1, l2, digits = passport[0], passport[1], passport[2:]
    l1_variants = PASSPORT_LETTER_VARIANTS.get(l1, [l1])
    l2_variants = PASSPORT_LETTER_VARIANTS.get(l2, [l2])
    variants = []
    for a in l1_variants:
        for b in l2_variants:
            variants.append(f"{a}{b}{digits}")
    return list(set(variants))



def get_df(path="bxm.xlsx"):
    global _df
    if _df is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base_dir, path)
        print(f"Loading BXM data from {path}...")
        _df = pd.read_excel(path, header=1)
        print("📦 bxm.xlsx loaded")
    return _df


PASSPORT_LETTER_VARIANTS = {
    'B': ['B', 'V', 'P', 'F', 'D'],
    'V': ['V', 'B'],
    'D': ['D', 'T'],
    'T': ['T', 'D', 'K'],
    'C': ['C', 'S', 'Z'],
    'S': ['S', 'C', 'Z'],
    'Z': ['Z', 'S', 'C'],
    'Q': ['Q', 'G', 'K'],
    'K': ['K', 'Q', 'G'],
    'I': ['I', 'Y', 'E'],
    'Y': ['Y', 'I', 'E'],
    'E': ['E', 'Y', 'I'],
    'F': ['F', 'P', 'B'],
    'P': ['P', 'F', 'B'],
    'X': ['X', 'H'],
    'H': ['H', 'X'],
    'U': ['U', 'V'],
}
