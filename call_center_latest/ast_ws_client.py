#!/usr/bin/env python3

from argparse import ArgumentParser as ArgParser
import asyncio
import json
import logging
import uuid

import concurrent

from ast_media_websocket import AstMediaWebSocketClient
from ast_ari_websocket import AstAriWebSocketClient
from llm.llm_social_payment import initialize_mcp
from worker_pool import WorkerPool

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(message)s",
    datefmt="[%Y-%m-%d %H:%M:%S]",
    level=logging.INFO,
)


# Tune these two numbers in one place.
NUM_WORKERS = 30
SESSIONS_PER_WORKER = 1


class session:
    def __init__(self, incoming, incoming_name):
        self.incoming_channel = incoming
        self.incoming_channel_name = incoming_name
        self.ws_channel = None
        self.ws_channel_name = None
        self.bridge_id = None
        self.media_connected = False
        self.live_session_id = None
        self.caller_num = None
        # Set when the Media WebSocket task ends so the worker slot is freed.
        self.media_done = asyncio.Event()


class ast_ws_client(AstAriWebSocketClient):
    def __init__(self, host, port, app, credentials, tag=None, log_level=logging.INFO):
        super().__init__(host, port, app, credentials, tag, log_level)
        self.sessions_by_incoming = {}
        self.sessions_by_websocket = {}
        self.tag = tag
        self.pool = WorkerPool(
            num_workers=NUM_WORKERS,
            max_sessions_per_worker=SESSIONS_PER_WORKER,
        )

    async def handle_stasisstart(self, msg):
        app_data = msg['channel']['dialplan']['app_data']
        channel_id = msg['channel']['id']

        if "incoming" in app_data:
            sess = session(channel_id, msg['channel']['name'])

            resp = await self.send_request(
                "GET", f"channels/{channel_id}/variable?variable=SESSION_ID"
            )
            body = json.loads(resp.get('message_body', '{}'))
            sess.live_session_id = body.get('value', '')

            resp2 = await self.send_request(
                "GET", f"channels/{channel_id}/variable?variable=CALLER_NUM"
            )
            body2 = json.loads(resp2.get('message_body', '{}'))
            sess.caller_num = body2.get('value', '')

            logger.info(
                f"Caller: {sess.caller_num}, Session: {sess.live_session_id} | "
                f"Pool {self.pool.status()}"
            )

            self.sessions_by_incoming[channel_id] = sess

            logger.info(f"Incoming call {channel_id}. Creating slin16 WebSocket.")
            resp = await self.send_request(
                "POST", "channels/create",
                query_strings=[
                    {"name": "endpoint", "value": "WebSocket/INCOMING/c(slin16)"},
                    {"name": "app", "value": self.app},
                    {"name": "appArgs", "value": "websocket"},
                    {"name": "originator", "value": channel_id},
                ],
            )

            msg_body = json.loads(resp.get('message_body'))
            sess.ws_channel = msg_body['id']
            sess.ws_channel_name = msg_body['name']
            self.sessions_by_websocket[sess.ws_channel] = sess

            logger.info(f"Dialing websocket channel {sess.ws_channel}")
            await self.send_request(
                "POST",
                f"channels/{sess.ws_channel}/dial?caller={channel_id}&timeout=5",
            )

        elif "websocket" in app_data:
            sess = self.sessions_by_websocket.get(channel_id)
            if sess:
                sess.bridge_id = str(uuid.uuid4())
                logger.info(f"Creating bridge {sess.bridge_id}")
                await self.send_request("POST", f"bridges/{sess.bridge_id}?type=mixing")
                await self.send_request(
                    "POST",
                    f"bridges/{sess.bridge_id}/addChannel?channel={sess.incoming_channel}",
                )
                await self.send_request(
                    "POST",
                    f"bridges/{sess.bridge_id}/addChannel?channel={sess.ws_channel}",
                )

    async def handle_dial(self, msg):
        peer = msg.get('peer')
        if not peer:
            return

        chan_id = peer['id']
        if "WebSocket/" not in peer['name']:
            return

        sess = self.sessions_by_websocket.get(chan_id)
        if not sess or sess.media_connected:
            return

        # 1) Avval channelvars dan urinib ko'ramiz (eng tez yo'l)
        conn_id = peer.get('channelvars', {}).get('MEDIA_WEBSOCKET_CONNECTION_ID')

        # 2) Topilmasa — variable so'rovi bilan, retry qilib
        if not conn_id:
            for attempt in range(5):
                resp = await self.send_request(
                    "GET",
                    f"channels/{chan_id}/variable?variable=MEDIA_WEBSOCKET_CONNECTION_ID",
                )
                conn_id = json.loads(resp.get('message_body', '{}')).get('value')
                if conn_id:
                    break
                await asyncio.sleep(0.2)

        if not conn_id:
            self.log(logging.ERROR, f"conn_id never appeared for {chan_id}")
            return

        sess.media_connected = True
        logger.info(
            f"Submitting Media WS to pool: conn={conn_id} "
            f"session_id={sess.live_session_id} | {self.pool.status()}"
        )

        # The coroutine factory is what the worker will run. The session_key
        # uniquely identifies this session inside the pool.
        session_key = chan_id
        host, port = self.host, self.port
        live_session_id = sess.live_session_id
        caller_num = sess.caller_num
        media_done = sess.media_done

        def make_coro():
            async def run():
                try:
                    mwc = AstMediaWebSocketClient(
                        host, port, conn_id,
                        tag=chan_id, log_level=logging.INFO,
                        session_id=live_session_id, caller_num=caller_num,
                    )
                    await mwc.connect()
                finally:
                    # Mark the session as finished so the worker slot frees.
                    media_done.set()
            return run()

        await self.pool.submit(session_key, make_coro)

    async def handle_stasisend(self, msg):
        channel_id = msg['channel']['id']
        app_data = msg['channel']['dialplan']['app_data']

        sess = None
        if "incoming" in app_data:
            sess = self.sessions_by_incoming.pop(channel_id, None)
            if sess and sess.ws_channel:
                logger.info(f"Cleaning up WebSocket {sess.ws_channel}")
                await self.send_request("DELETE", f"channels/{sess.ws_channel}")
        elif "websocket" in app_data:
            sess = self.sessions_by_websocket.pop(channel_id, None)
            if sess and sess.incoming_channel:
                logger.info(f"Cleaning up SIP Channel {sess.incoming_channel}")
                await self.send_request("DELETE", f"channels/{sess.incoming_channel}")

        if sess and sess.bridge_id:
            logger.info(f"Destroying bridge {sess.bridge_id}")
            await self.send_request("DELETE", f"bridges/{sess.bridge_id}")

        if sess:
            logger.info(f"Pool status after end: {self.pool.status()}")



async def status_logger(pool, interval=5):
    while True:
        await asyncio.sleep(interval)
        logger.info(f"[POOL STATUS] {pool.status()}")


async def main(args):
    loop = asyncio.get_running_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=128))
    await initialize_mcp()
    event_handler = ast_ws_client(
        args.ari_host, args.ari_port, args.stasis_app,
        (args.ari_user, args.ari_password),
    )
    asyncio.create_task(status_logger(event_handler.pool, interval=5))
    await event_handler.connect()

if __name__ == "__main__":
    parser = ArgParser()
    parser.add_argument("-ah", "--ari-host", default="localhost")
    parser.add_argument("-ap", "--ari-port", default="8088")
    parser.add_argument("-a", "--stasis-app", required=True)
    parser.add_argument("-aU", "--ari-user", required=True)
    parser.add_argument("-aP", "--ari-password", required=True)
    asyncio.run(main(parser.parse_args()))