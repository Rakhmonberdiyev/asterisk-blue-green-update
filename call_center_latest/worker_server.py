#!/usr/bin/env python3
"""
Worker server: HTTP server that accepts media session assignments from ari-gateway,
then connects to Asterisk media WebSocket (ws://asterisk:8088/media/{conn_id})
and processes STT/LLM/TTS.
"""

import argparse
import asyncio
import concurrent.futures
import logging
import os

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from ast_media_websocket import AstMediaWebSocketClient
from llm.llm_social_payment import initialize_mcp

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(message)s",
    datefmt="[%Y-%m-%d %H:%M:%S]",
    level=logging.INFO,
)

# active_sessions: conn_id -> asyncio.Task
_active_sessions: dict[str, asyncio.Task] = {}
_max_sessions = 2
_lock = asyncio.Lock()


async def _add_to_bridge(asterisk_host: str, asterisk_port: str,
                         bridge_id: str, ws_channel_id: str):
    """Add the WebSocket channel to the bridge after Python has connected."""
    ari_user = os.getenv("ARI_USER", "ai-user")
    ari_pass = os.getenv("ARI_PASSWORD", "your_password")
    url = f"http://{asterisk_host}:{asterisk_port}/ari/bridges/{bridge_id}/addChannel?channel={ws_channel_id}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, auth=aiohttp.BasicAuth(ari_user, ari_pass),
                              timeout=aiohttp.ClientTimeout(total=5)) as resp:
                logger.info("addChannel bridge=%s chan=%s → %d", bridge_id, ws_channel_id, resp.status)
    except Exception:
        logger.exception("Failed to add WS channel to bridge")


async def _run_media_session(conn_id: str, asterisk_host: str, asterisk_port: str,
                              session_id: str, caller_num: str,
                              bridge_id: str = "", ws_channel_id: str = ""):
    try:
        on_connected = None
        if bridge_id and ws_channel_id:
            async def on_connected():
                await _add_to_bridge(asterisk_host, asterisk_port, bridge_id, ws_channel_id)

        client = AstMediaWebSocketClient(
            asterisk_host, asterisk_port, conn_id,
            tag=conn_id, log_level=logging.INFO,
            session_id=session_id, caller_num=caller_num,
            on_connected=on_connected,
        )
        await client.connect()
    except Exception:
        logger.exception("Media session error for conn=%s", conn_id)
    finally:
        async with _lock:
            _active_sessions.pop(conn_id, None)
        logger.info("Session finished conn=%s active=%d", conn_id, len(_active_sessions))


async def handle_start_session(request: web.Request) -> web.Response:
    global _max_sessions
    payload = await request.json()
    conn_id = payload.get("connection_id")
    if not conn_id:
        return web.json_response({"error": "connection_id required"}, status=400)

    asterisk_host  = payload.get("asterisk_host", os.getenv("ARI_HOST", "asterisk"))
    asterisk_port  = str(payload.get("asterisk_port", os.getenv("ARI_PORT", "8088")))
    session_id     = payload.get("session_id", "")
    caller_num     = payload.get("caller_num", "unknown")
    bridge_id      = payload.get("bridge_id", "")
    ws_channel_id  = payload.get("ws_channel_id", "")

    async with _lock:
        if len(_active_sessions) >= _max_sessions:
            return web.json_response(
                {"error": "worker_full", "active": len(_active_sessions), "max": _max_sessions},
                status=503,
            )
        task = asyncio.create_task(
            _run_media_session(conn_id, asterisk_host, asterisk_port, session_id, caller_num,
                               bridge_id, ws_channel_id)
        )
        _active_sessions[conn_id] = task

    logger.info("Accepted conn=%s caller=%s active=%d/%d",
                conn_id, caller_num, len(_active_sessions), _max_sessions)
    return web.json_response({"status": "accepted", "conn_id": conn_id}, status=200)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "active_sessions": len(_active_sessions),
        "max_sessions": _max_sessions,
        "worker": os.getenv("HOSTNAME", "unknown"),
    })


async def on_startup(app):
    if os.getenv("INIT_MCP", "1") == "1":
        await initialize_mcp()
    loop = asyncio.get_running_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=64))


def build_app(max_sessions: int) -> web.Application:
    global _max_sessions
    _max_sessions = max_sessions
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_post("/start-session", handle_start_session)
    app.on_startup.append(on_startup)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host",         default=os.getenv("WORKER_HOST", "0.0.0.0"))
    parser.add_argument("--port",         type=int, default=int(os.getenv("WORKER_PORT", "8766")))
    parser.add_argument("--max-sessions", type=int, default=int(os.getenv("WORKER_MAX_SESSIONS", "2")))
    args = parser.parse_args()
    web.run_app(build_app(args.max_sessions), host=args.host, port=args.port, access_log=None)
