#!/usr/bin/env python3

from argparse import ArgumentParser as ArgParser
import asyncio
import concurrent.futures
import json
import logging
import os
import uuid

import aiohttp

from ast_ari_websocket import AstAriWebSocketClient

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(message)s",
    datefmt="[%Y-%m-%d %H:%M:%S]",
    level=logging.INFO,
)


class Session:
    def __init__(self, incoming, incoming_name):
        self.incoming_channel = incoming
        self.incoming_channel_name = incoming_name
        self.ws_channel = None
        self.ws_channel_name = None
        self.bridge_id = None
        self.media_connected = False
        self.live_session_id = None
        self.caller_num = None
        self.cleaned = False


class AriGateway(AstAriWebSocketClient):
    def __init__(self, host, port, app, credentials, worker_balancer_url,
                 tag=None, log_level=logging.INFO):
        super().__init__(host, port, app, credentials, tag, log_level)
        self.sessions_by_incoming = {}
        self.sessions_by_websocket = {}
        self.worker_balancer_url = worker_balancer_url.rstrip("/")
        self.http_session = None

    async def handle_stasisstart(self, msg):
        app_data = msg["channel"]["dialplan"]["app_data"]
        channel_id = msg["channel"]["id"]

        if "incoming" in app_data:
            sess = Session(channel_id, msg["channel"]["name"])

            resp = await self.send_request("GET", f"channels/{channel_id}/variable?variable=SESSION_ID")
            sess.live_session_id = json.loads(resp.get("message_body", "{}")).get("value", "")

            resp2 = await self.send_request("GET", f"channels/{channel_id}/variable?variable=CALLER_NUM")
            sess.caller_num = json.loads(resp2.get("message_body", "{}")).get("value", "")
            if not sess.caller_num:
                sess.caller_num = msg["channel"].get("caller", {}).get("number", "unknown")
            if not sess.live_session_id:
                sess.live_session_id = channel_id

            logger.info("Incoming call channel=%s caller=%s", channel_id, sess.caller_num)
            self.sessions_by_incoming[channel_id] = sess

            # Create slin16 WebSocket media channel (same approach as ast_ws_client.py)
            resp = await self.send_request(
                "POST", "channels/create",
                query_strings=[
                    {"name": "endpoint",  "value": "WebSocket/INCOMING/c(slin16)"},
                    {"name": "app",       "value": self.app},
                    {"name": "appArgs",   "value": "websocket"},
                    {"name": "originator","value": channel_id},
                ],
            )
            msg_body = json.loads(resp.get("message_body"))
            sess.ws_channel = msg_body["id"]
            sess.ws_channel_name = msg_body["name"]
            self.sessions_by_websocket[sess.ws_channel] = sess

            await self.send_request("POST", f"channels/{sess.ws_channel}/dial?caller={channel_id}&timeout=5")

        elif "websocket" in app_data:
            sess = self.sessions_by_websocket.get(channel_id)
            if sess:
                sess.bridge_id = str(uuid.uuid4())
                logger.info("Creating bridge %s", sess.bridge_id)
                await self.send_request("POST", f"bridges/{sess.bridge_id}?type=mixing")
                # Add only the SIP channel now. The WebSocket channel is added
                # by the worker AFTER it connects to the media WebSocket,
                # to avoid webchan_write errors from Asterisk.
                await self.send_request("POST", f"bridges/{sess.bridge_id}/addChannel?channel={sess.incoming_channel}")

    async def handle_dial(self, msg):
        peer = msg.get("peer")
        if not peer:
            return

        chan_id = peer["id"]
        if "WebSocket/" not in peer["name"]:
            return

        sess = self.sessions_by_websocket.get(chan_id)
        if not sess or sess.media_connected:
            return

        conn_id = peer.get("channelvars", {}).get("MEDIA_WEBSOCKET_CONNECTION_ID")
        if not conn_id:
            resp = await self.send_request("GET", f"channels/{chan_id}/variable?variable=MEDIA_WEBSOCKET_CONNECTION_ID")
            conn_id = json.loads(resp.get("message_body", "{}")).get("value")

        if not conn_id:
            return

        sess.media_connected = True
        logger.info("Dispatching conn=%s to worker balancer", conn_id)

        payload = {
            "connection_id": conn_id,
            "session_id":    sess.live_session_id,
            "caller_num":    sess.caller_num,
            "asterisk_host": self.host,
            "asterisk_port": self.port,
            "bridge_id":     sess.bridge_id,
            "ws_channel_id": sess.ws_channel,
        }
        try:
            async with self.http_session.post(
                f"{self.worker_balancer_url}/start-session",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                body = await resp.text()
                if resp.status >= 300:
                    raise RuntimeError(f"Worker rejected: {resp.status} {body}")
                logger.info("Worker accepted conn=%s", conn_id)
        except Exception:
            logger.exception("Failed to dispatch to worker; hanging up")
            await self.send_request("DELETE", f"channels/{sess.incoming_channel}")

    async def handle_stasisend(self, msg):
        channel_id = msg["channel"]["id"]
        app_data = msg["channel"]["dialplan"]["app_data"]

        if "incoming" in app_data:
            sess = self.sessions_by_incoming.get(channel_id)
        elif "websocket" in app_data:
            sess = self.sessions_by_websocket.get(channel_id)
        else:
            sess = None

        if not sess or sess.cleaned:
            return
        sess.cleaned = True

        self.sessions_by_incoming.pop(sess.incoming_channel, None)
        if sess.ws_channel:
            self.sessions_by_websocket.pop(sess.ws_channel, None)

        if "incoming" in app_data and sess.ws_channel:
            await self.send_request("DELETE", f"channels/{sess.ws_channel}")
        elif "websocket" in app_data and sess.incoming_channel:
            await self.send_request("DELETE", f"channels/{sess.incoming_channel}")

        if sess.bridge_id:
            await self.send_request("DELETE", f"bridges/{sess.bridge_id}")

    async def connect(self):
        async with aiohttp.ClientSession() as http:
            self.http_session = http
            await super().connect()


async def main(args):
    loop = asyncio.get_running_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=64))

    gateway = AriGateway(
        args.ari_host, args.ari_port, args.stasis_app,
        (args.ari_user, args.ari_password),
        args.worker_balancer_url,
    )
    while True:
        try:
            await gateway.connect()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ARI connection lost; retrying in 2s")
            await asyncio.sleep(2)


if __name__ == "__main__":
    parser = ArgParser()
    parser.add_argument("-ah", "--ari-host",           default=os.getenv("ARI_HOST", "asterisk"))
    parser.add_argument("-ap", "--ari-port",           default=os.getenv("ARI_PORT", "8088"))
    parser.add_argument("-a",  "--stasis-app",         default=os.getenv("ARI_APP"))
    parser.add_argument("-aU", "--ari-user",           default=os.getenv("ARI_USER"))
    parser.add_argument("-aP", "--ari-password",       default=os.getenv("ARI_PASSWORD"))
    parser.add_argument("-w",  "--worker-balancer-url",default=os.getenv("WORKER_BALANCER_URL", "http://nginx-workers"))
    args = parser.parse_args()
    if not args.stasis_app or not args.ari_user or not args.ari_password:
        parser.error("ARI app, user, and password are required")
    asyncio.run(main(args))
