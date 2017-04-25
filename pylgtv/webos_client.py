import asyncio
import base64
import codecs
import json
import os
import websockets
import logging

logger = logging.getLogger(__name__)

from .endpoints import *

KEY_FILE_NAME = '.pylgtv'
USER_HOME = 'HOME'
HANDSHAKE_FILE_NAME = 'handshake.json'


class PyLGTVPairException(Exception):
    def __init__(self, id, message):
        self.id = id
        self.message = message


class WebOsClient(object):
    def __init__(self, ip, key_file_path=None, timeout_connect=2):
        """Initialize the client."""
        self.ip = ip
        self.port = 3000
        self.key_file_path = key_file_path
        self.client_key = None
        self.web_socket = None
        self.command_count = 0
        self.last_response = None
        self.timeout_connect = timeout_connect

        self.load_key_file()

    @staticmethod
    def _get_key_file_path():
        """Return the key file path."""
        if os.getenv(USER_HOME) is not None and os.access(os.getenv(USER_HOME),
                                                          os.W_OK):
            return os.path.join(os.getenv(USER_HOME), KEY_FILE_NAME)

        return os.path.join(os.getcwd(), KEY_FILE_NAME)

    def load_key_file(self):
        """Try to load the client key for the current ip."""
        self.client_key = None
        if self.key_file_path:
            key_file_path = self.key_file_path
        else:
            key_file_path = self._get_key_file_path()
        key_dict = {}

        logger.debug('load keyfile from %s', key_file_path);

        if os.path.isfile(key_file_path):
            with open(key_file_path, 'r') as f:
                raw_data = f.read()
                if raw_data:
                    key_dict = json.loads(raw_data)

        logger.debug('getting client_key for %s from %s', self.ip, key_file_path);
        if self.ip in key_dict:
            self.client_key = key_dict[self.ip]

    def save_key_file(self):
        """Save the current client key."""
        if self.client_key is None:
            return

        if self.key_file_path:
            key_file_path = self.key_file_path
        else:
            key_file_path = self._get_key_file_path()

        logger.debug('save keyfile to %s', key_file_path);

        with open(key_file_path, 'w+') as f:
            raw_data = f.read()
            key_dict = {}

            if raw_data:
                key_dict = json.loads(raw_data)

            key_dict[self.ip] = self.client_key

            f.write(json.dumps(key_dict))

    @asyncio.coroutine
    def _send_register_payload(self, websocket):
        """Send the register payload."""
        file = os.path.join(os.path.dirname(__file__), HANDSHAKE_FILE_NAME)

        data = codecs.open(file, 'r', 'utf-8')
        raw_handshake = data.read()

        handshake = json.loads(raw_handshake)
        handshake['payload']['client-key'] = self.client_key

        yield from websocket.send(json.dumps(handshake))
        raw_response = yield from websocket.recv()
        response = json.loads(raw_response)

        if response['type'] == 'response' and \
                        response['payload']['pairingType'] == 'PROMPT':
            raw_response = yield from websocket.recv()
            response = json.loads(raw_response)
            if response['type'] == 'registered':
                self.client_key = response['payload']['client-key']
                self.save_key_file()

    def is_registered(self):
        """Paired with the tv."""
        return self.client_key is not None

    @asyncio.coroutine
    def _register(self):
        """Register wrapper."""
        logger.debug('register on %s', "ws://{}:{}".format(self.ip, self.port));
        try:
            websocket = yield from websockets.connect(
                "ws://{}:{}".format(self.ip, self.port), timeout=self.timeout_connect)

        except:
            logger.error('register failed to connect to %s', "ws://{}:{}".format(self.ip, self.port));
            return False

        logger.debug('register websocket connected to %s', "ws://{}:{}".format(self.ip, self.port));

        try:
            yield from self._send_register_payload(websocket)

        finally:
            logger.debug('close register connection to %s', "ws://{}:{}".format(self.ip, self.port));
            yield from websocket.close()

    def register(self):
        """Pair client with tv."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._register())

    @asyncio.coroutine
    def _command(self, msg):
        """Send a command to the tv."""
        logger.debug('send command to %s', "ws://{}:{}".format(self.ip, self.port));
        try:
            websocket = yield from websockets.connect(
                "ws://{}:{}".format(self.ip, self.port), timeout=self.timeout_connect)
        except:
            logger.debug('command failed to connect to %s', "ws://{}:{}".format(self.ip, self.port));
            return False

        logger.debug('command websocket connected to %s', "ws://{}:{}".format(self.ip, self.port));

        try:
            yield from self._send_register_payload(websocket)

            if not self.client_key:
                raise PyLGTVPairException("Unable to pair")

            yield from websocket.send(json.dumps(msg))

            if msg['type'] == 'request':
                raw_response = yield from websocket.recv()
                self.last_response = json.loads(raw_response)

        finally:
            logger.debug('close command connection to %s', "ws://{}:{}".format(self.ip, self.port));
            yield from websocket.close()

    def command(self, request_type, uri, payload):
        """Build and send a command."""
        self.command_count += 1

        if payload is None:
            payload = {}

        message = {
            'id': "{}_{}".format(type, self.command_count),
            'type': request_type,
            'uri': "ssap://{}".format(uri),
            'payload': payload,
        }

        self.last_response = None

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(asyncio.wait_for(self._command(message), self.timeout_connect, loop=loop))
        finally:
            loop.close()

    def request(self, uri, payload=None):
        """Send a request."""
        self.command('request', uri, payload)

    def send_message(self, message, icon_path=None):
        """Show a floating message."""
        icon_encoded_string = ''
        icon_extension = ''

        if icon_path is not None:
            icon_extension = os.path.splitext(icon_path)[1][1:]
            with open(icon_path, 'rb') as icon_file:
                icon_encoded_string = base64.b64encode(icon_file.read()).decode('ascii')

        self.request(EP_SHOW_MESSAGE, {
            'message': message,
            'iconData': icon_encoded_string,
            'iconExtension': icon_extension
        })

    # Apps
    def get_apps(self):
        """Return all apps."""
        self.request(EP_GET_APPS)
        return {} if self.last_response is None else self.last_response.get('payload').get('launchPoints')

    def get_current_app(self):
        """Get the current app id."""
        self.request(EP_GET_CURRENT_APP_INFO)
        return None if self.last_response is None else self.last_response.get('payload').get('appId')

    def launch_app(self, app):
        """Launch an app."""
        self.command('request', EP_LAUNCH, {
            'id': app
        })

    def launch_app_with_params(self, app, params):
        """Launch an app with parameters."""
        self.request(EP_LAUNCH, {
            'id': app,
            'params': params
        })

    def close_app(self, app):
        """Close the current app."""
        self.request(EP_LAUNCHER_CLOSE, {
            'id': app
        })

    # Services
    def get_services(self):
        """Get all services."""
        self.request(EP_GET_SERVICES)
        return {} if self.last_response is None else self.last_response.get('payload').get('services')

    def get_software_info(self):
        """Return the current software status."""
        self.request(EP_GET_SOFTWARE_INFO)
        return {} if self.last_response is None else self.last_response.get('payload')

    def power_off(self):
        """Play media."""
        self.request(EP_POWER_OFF)

    # 3D Mode
    def turn_3d_on(self):
        """Turn 3D on."""
        self.request(EP_3D_ON)

    def turn_3d_off(self):
        """Turn 3D off."""
        self.request(EP_3D_OFF)

    # Inputs
    def get_inputs(self):
        """Get all inputs."""
        self.request(EP_GET_INPUTS)
        return {} if self.last_response is None else self.last_response.get('payload').get('devices')

    def get_input(self):
        """Get current input."""
        return self.get_current_app()

    def set_input(self, input):
        """Set the current input."""
        self.request(EP_SET_INPUT, {
            'inputId': input
        })

    # Audio
    def get_audio_status(self):
        """Get the current audio status"""
        self.request(EP_GET_AUDIO_STATUS)
        return {} if self.last_response is None else self.last_response.get('payload')

    def get_muted(self):
        """Get mute status."""
        return self.get_audio_status().get('mute')

    def set_mute(self, mute):
        """Set mute."""
        self.request(EP_SET_MUTE, {
            'mute': mute
        })

    def get_volume(self):
        """Get the current volume."""
        self.request(EP_GET_VOLUME)
        return 0 if self.last_response is None else self.last_response.get('payload').get('volume')

    def set_volume(self, volume):
        """Set volume."""
        volume = max(0, volume)
        self.request(EP_SET_VOLUME, {
            'volume': volume
        })

    def volume_up(self):
        """Volume up."""
        self.request(EP_VOLUME_UP)

    def volume_down(self):
        """Volume down."""
        self.request(EP_VOLUME_DOWN)

    # TV Channel
    def channel_up(self):
        """Channel up."""
        self.request(EP_TV_CHANNEL_UP)

    def channel_down(self):
        """Channel down."""
        self.request(EP_TV_CHANNEL_DOWN)

    def get_channels(self):
        """Get all tv channels."""
        self.request(EP_GET_TV_CHANNELS)
        return {} if self.last_response is None else self.last_response.get('payload').get('channelList')

    def get_current_channel(self):
        """Get the current tv channel."""
        self.request(EP_GET_CURRENT_CHANNEL)
        return {} if self.last_response is None else self.last_response.get('payload')

    def get_channel_info(self):
        """Get the current channel info."""
        self.request(EP_GET_CHANNEL_INFO)
        return {} if self.last_response is None else self.last_response.get('payload')

    def set_channel(self, channel):
        """Set the current channel."""
        self.request(EP_SET_CHANNEL, {
            'channelId': channel
        })

    # Media control
    def play(self):
        """Play media."""
        self.request(EP_MEDIA_PLAY)

    def pause(self):
        """Pause media."""
        self.request(EP_MEDIA_PAUSE)

    def stop(self):
        """Stop media."""
        self.request(EP_MEDIA_STOP)

    def close(self):
        """Close media."""
        self.request(EP_MEDIA_CLOSE)

    def rewind(self):
        """Rewind media."""
        self.request(EP_MEDIA_REWIND)

    def fast_forward(self):
        """Fast Forward media."""
        self.request(EP_MEDIA_FAST_FORWARD)

    # Keys
    def send_enter_key(self):
        """Send enter key."""
        self.request(EP_SEND_ENTER)

    def send_delete_key(self):
        """Send delete key."""
        self.request(EP_SEND_DELETE)

    # Web
    def open_url(self, url):
        """Open URL."""
        self.request(EP_OPEN, {
            'target': url
        })

    def close_web(self):
        """Close web app."""
        self.request(EP_CLOSE_WEB_APP)
