"""
Copyright (C) 2025, Sangoma Technologies Corporation
George T Joseph <gjoseph@sangoma.com>

This program is free software, distributed under the terms of
the Apache License Version 2.0.
"""

import asyncio
import json
import logging
from logging import INFO, WARNING, ERROR, DEBUG, NOTSET
import signal
import uuid
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.asyncio.server import basic_auth

class AstAriWebSocket:
    def __init__(self, tag=None, log_level=None):
        """
        Initializes the ARI event handler.
        :param host: The host address of the Asterisk server for client connections
        or the local address to bind to for server connections.
        :param port: The port number of the Asterisk ARI interface for client connections
        or the port number to bind to for server connections.
        :param credentials: A tuple containing the username and password for ARI authentication.
        :param app: The ARI application name to connect to for client connections.
        :param protocol: The protocol to use for the WebSocket server connection. Default "ari".
        """
        self.requests = {}
        self.logger = logging.getLogger(__name__)
        self.tag = tag
        if log_level is not None:
            self.logger.setLevel(log_level)

    def log(self, level, message):
        """
        Logs a message with the tag.
        :param level: The logging level (e.g., info, warning, error).
        :param message: The message to log.
        """
        tag = "" if self.tag is None else f"{self.tag}: "
        self.logger.log(level, f"{tag}{message}")

    async def send_request(self, method, uri, wait_for_response=True, callback=None, **kwargs):
        """
        Sends a REST request over the WebSocket connection.
        :param method: The HTTP method (GET, POST, etc.) to use for the request.
        :param uri: The URI for the REST request.
        :param wait_for_response: Whether to wait for a response from the server.
        :param callback: An optional callback function to process the response.
        :param kwargs: Additional parameters to include in the request.
        :return: The response from the server, or the result of the callback function.
        """
        uuidstr = kwargs.pop('request_id', str(uuid.uuid4()))
        req = {
            'type': 'RESTRequest',
            'request_id': uuidstr,
            'method': method,
            'uri': uri
        }
    
        for k,v in kwargs.items():
            req[k] = v
    
        msg = json.dumps(req)
        rtnobj = { 'result': ''}
        if wait_for_response:
            rtnobj['event'] = asyncio.Event()
    
        self.requests[uuidstr] = rtnobj
        self.log(INFO, f"RESTRequest: {method} {uri} {uuidstr}")
        await self.websocket.send(msg.encode('utf-8'), text=True)
        if wait_for_response:
            await rtnobj['event'].wait()
        del self.requests[uuidstr]
        resp = rtnobj['result']
        self.log(INFO, f"RESTResponse: {method} {uri} {resp['status_code']} {resp['reason_phrase']}")
        if callback is not None:
            return callback(self.websocket, uuidstr, req, rtnobj['result'])
        return rtnobj['result']
    
    async def process_rest_response(self, msg):
        """
        Processes a REST response message received over the WebSocket.
        :param msg: The REST response message.
        """
        if msg['type'] == "RESTResponse":
            reqid = msg['request_id']
            req = self.requests.get(reqid)
            if req is None:
                self.log(ERROR, f"Pending request {reqid} not found.")
                return
            req['result'] = msg
            event = req.get('event', None)
            if event is not None:
                event.set()

    def get_function(self, func):
        """
        Returns a callable function based on the provided function name.
        :param func: The name of the function to retrieve.
        :return: The callable function if it exists, otherwise None.
        """
        if hasattr(self, func):
            attr = getattr(self, func)
            if callable(attr):
                return attr
            return None

    async def handle_any(self, msg):
        """
        Handles any ARI event that does not have a specific handler.
        :param rest_handler: The REST handler to use for processing the event.
        :param msg: The ARI event message.
        """
        if msg['type'] == "ChannelVarset":
            return
        et = msg['type']
        ts = msg['timestamp']
        del msg['timestamp']
        del msg['type']
        msg['timestamp'] = ts
        name = ""
        if 'bridge' in msg:
            name = msg['bridge']['name'] if len(msg['bridge']['name']) > 0 else msg['bridge']['id']
        if 'channel' in msg:
            name += f" {msg['channel']['name']}"

        self.log(INFO, f"Received {et} {name}")

    async def process_message(self, msg):
        """
        Processes an incoming ARI event message and call a handler if it exists.
        :param msg: The ARI event message to process.
        """
        if msg['type'] == "RESTResponse":
            await self.process_rest_response(msg)
            return
        handler_name = f"handle_{msg['type'].lower()}"
        func = self.get_function(handler_name)
        await self.handle_any(msg)
        if func is not None:
            await func(msg)

    async def handle_connection(self, websocket):
        """
        Handles the WebSocket connection and processes incoming messages.
        :param websocket: The WebSocket connection to handle.
        """
        self.log(INFO, f"ARI websocket connection from {websocket.remote_address}")
        self.websocket = websocket
        async for message in websocket:
            msg = json.loads(message)
            asyncio.create_task(self.process_message(msg))
        self.log(INFO, f"ARI disconnected")

class AstAriWebSocketServer(AstAriWebSocket):
    def __init__(self, host, port, credentials, protocol="ari",
                 tag=None, log_level=None):
        """
        Initializes the media websocket server.
        :param host: The host address to bind the server to.
        :param port: The port to bind the server to.
        :param credentials: Optional credential tuple for authentication ("username", "password")
        :param protocol: The protocol to use for the websocket. Default "ari".
        :param tag: Optional tag for logging.
        :param log_level: Optional logging level for the server.
        """
        super().__init__(tag, log_level)
        self.host = host
        self.port = port
        self.credentials = credentials
        self.protocol = protocol
        self.websocket = None
        self.server = None

    async def listen(self):
        """
        Starts the ARI WebSocket server and listens for incoming connections.
        This method is used for server connections from Asterisk.
        """
        self.log(INFO, f"Starting ARI server")
        auth = None
        if self.credentials is not None:
            auth =  basic_auth(realm="asterisk", credentials=self.credentials)

        async with serve(self.handle_connection, self.host, self.port,
                         subprotocols=[self.protocol],
                         process_request=auth) as server:
            self.server = server
            await server.wait_closed()

    async def stop(self):
        if self.server is not None:
            self.log(INFO, "Stopping ARI server")
            self.server.close()
            await self.server.wait_closed()
            self.server = None

class AstAriWebSocketClient(AstAriWebSocket):
    def __init__(self, host, port, app, credentials, tag=None, log_level=None):
        """
        Initializes the media websocket client.
        :param uri: The URI to connect to the media websocket.
        :param tag: Optional tag for logging.
        """
        super().__init__(tag, log_level)
        self.host = host
        self.port = port
        self.app = app
        self.credentials = credentials

    async def connect(self):
        """
        Connects to the ARI WebSocket server.
        This method is used for client connections to the Asterisk ARI server.
        """
        uri = f"ws://{self.host}:{self.port}/ari/events?subscribeAll=false&app={self.app}&api_key={':'.join(self.credentials)}"
        self.log(INFO, f"Connecting to ARI at {uri}")
        async with connect(uri) as websocket:
            await self.handle_connection(websocket)

