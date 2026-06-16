#!/usr/bin/env python

"""
Copyright (C) 2025, Sangoma Technologies Corporation
George T Joseph <gjoseph@sangoma.com>

This program is free software, distributed under the terms of
the Apache License Version 2.0.
"""

from argparse import ArgumentParser as ArgParser
import asyncio
import logging
import sys
import uuid
import traceback
from ast_media_websocket import AstMediaWebSocketServer
from ast_ari_websocket import AstAriWebSocketServer

logger = logging.getLogger(__name__)

logging.basicConfig(
    format="[%(asctime)s][%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)

class session():
    def __init__(self, incoming, incoming_name):
        self.incoming_channel = incoming
        self.incoming_channel_name = incoming_name
        self.ws_channel = None
        self.ws_channel_name = None
        self.bridge_id = None
        self.conn_id = None
        self.mws = None

class ast_ws_server(AstAriWebSocketServer):
    def __init__(self, ari_host, ari_port, ari_credentials, ari_protocol,
                 media_host, media_port, media_credentials, media_protocol,
                 tag=None, log_level=logging.INFO):

        super().__init__(ari_host, ari_port, ari_credentials, ari_protocol, tag, log_level)
        self.sessions_by_incoming = {}
        self.sessions_by_websocket = {}
        self.media_host = media_host
        self.media_port = media_port
        self.media_credentials = media_credentials
        self.media_protocol = media_protocol

        self.mws = AstMediaWebSocketServer(self.media_host,
                      self.media_port, self.media_credentials,
                      self.media_protocol,
                      tag = tag,
                      log_level=log_level)
        asyncio.create_task(self.mws.listen())

    async def handle_stasisstart(self, msg):
        if "incoming" in msg['channel']['dialplan']['app_data']:
            incoming_id = msg['channel']['id']
            sess = session(incoming_id, msg['channel']['name'])
            self.sessions_by_incoming[incoming_id] = sess
            logger.info("Creating websocket channel")
            
            sess.ws_channel = str(uuid.uuid4())
            self.sessions_by_websocket[sess.ws_channel] = sess
            resp = await self.send_request("POST", "channels/externalMedia", 
                      query_strings=[
                      { "name": "channelId", "value": sess.ws_channel },
                      { "name": "app", "value": msg['application'] },
                      { "name": "data", "value": "websocket" },
                      { "name": "external_host", "value": "media_connection1" },
                      { "name": "transport", "value": "websocket" },
                      { "name": "encapsulation", "value": "none" },
                      { "name": "format", "value": "ulaw" },
                      ])

        if "websocket" in msg['channel']['dialplan']['app_data']:
            sess = self.sessions_by_websocket.get(msg['channel']['id'])
            sess.ws_channel_name = msg['channel']['name']

    async def handle_dial(self, msg):
        chan_name = msg['peer']['name']
        if "WebSocket/" not in chan_name:
            return
        logger.info(f"Dial: {chan_name} Status: '{msg['dialstatus']}' websocket")

        if msg['dialstatus'] == "ANSWER":
            sess = self.sessions_by_websocket.get(msg['peer']['id'])
            sess.bridge_id = str(uuid.uuid4())
            logger.info(f"Creating bridge {sess.bridge_id}")
            resp = await self.send_request("POST", f"bridges/{sess.bridge_id}?type=mixing")
            await self.send_request("POST", f"bridges/{sess.bridge_id}/addChannel?channel={sess.incoming_channel}")
            await self.send_request("POST", f"bridges/{sess.bridge_id}/addChannel?channel={sess.ws_channel}")

            logger.info(f"Answering {sess.incoming_channel_name}")
            resp = await self.send_request("POST", f"channels/{sess.incoming_channel}/answer")

    async def handle_stasisend(self, msg):
        sess = None
        if "incoming" in msg['channel']['dialplan']['app_data']:
            sess = self.sessions_by_incoming.get(msg['channel']['id'])
            if sess is not None:
                if sess.ws_channel is not None:
                    logger.info(f"Hanging up {sess.ws_channel_name}")
                    await self.send_request("DELETE", f"channels/{sess.ws_channel}")
                del self.sessions_by_incoming[sess.incoming_channel]
                sess.incoming_channel = None
        if "websocket" in msg['channel']['dialplan']['app_data']:
            sess = self.sessions_by_websocket.get(msg['channel']['id'])
            if sess is not None:
                if sess.incoming_channel is not None:
                    logger.info(f"Hanging up {sess.incoming_channel_name}")
                    await self.send_request("DELETE", f"channels/{sess.incoming_channel}")
                del self.sessions_by_websocket[sess.ws_channel]
                sess.ws_channel = None

        if sess is not None and sess.bridge_id is not None:
            await self.send_request("DELETE", f"bridges/{sess.bridge_id}")
            sess.bridge_id = None

async def main(args):

    ari_creds = None
    media_creds = None
    if args.ari_user is not None and args.ari_password is not None:
        ari_creds = (args.ari_user, args.ari_password)
    if args.media_user is not None and args.media_password is not None:
        media_creds = (args.media_user, args.media_password)
        
    event_handler = ast_ws_server(args.ari_bind_address, args.ari_bind_port,
                    ari_creds,
                    args.ari_websocket_protocol,
                    args.media_bind_address,
                    args.media_bind_port,
                    media_creds,
                    args.media_websocket_protocol,
                    tag="ari_ws_server",
                    log_level=logging.INFO
                    )
    try:
        await event_handler.listen()
    except Exception as e:
        traceback.print_exc()
        return

if __name__ == "__main__":
    description = (
        'Command line utility to test ARI and media websocket client connections'
    )

    parser = ArgParser(description=description)
    parser.add_argument("-aa", "--ari-bind-address", type=str, help="Address to bind ARI websocket server to. Default=localhost", default="localhost", required=False)
    parser.add_argument("-ap", "--ari-bind-port", type=str, help="Port bind ARI websocket server to", required=True)
    parser.add_argument("-awp", "--ari-websocket-protocol", type=str, help="Protocol for ARI websocket. Default=ari", required=False, default="ari")
    parser.add_argument("-aU", "--ari-user", type=str, help="ARI user to authenticate incoming connections against", required=False)
    parser.add_argument("-aP", "--ari-password", type=str, help="Password for ARI user", required=False)
    parser.add_argument("-ma", "--media-bind-address", type=str, help="Address to bind Media websocket server to. Default=localhost", default="localhost", required=False)
    parser.add_argument("-mp", "--media-bind-port", type=str, help="Port to bind media websocket to", required=True)
    parser.add_argument("-mwp", "--media-websocket-protocol", type=str, help="Protocol for Media websocket. Default=media", required=False, default="media")
    parser.add_argument("-mwi", "--media-websocket-id", type=str, help="Connection name from websocket_client.conf. Default=media_connection1", required=False, default="media_connection1")
    parser.add_argument("-mU", "--media-user", type=str, help="ARI user to authenticate incoming connections against", required=False)
    parser.add_argument("-mP", "--media-password", type=str, help="Password for Media user", required=False)
    args = parser.parse_args()
    if not args:
        print("No args!!!", flush=True)
        sys.exit(1)
    
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
    sys.exit(0)
