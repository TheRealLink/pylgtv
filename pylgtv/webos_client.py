import asyncio
import base64
import codecs
import json
import os
import websockets
import logging
from uuid import uuid4

logger = logging.getLogger(__name__)

from .endpoints import *

KEY_FILE_NAME = '.pylgtv'
USER_HOME = 'HOME'
HANDSHAKE_FILE_NAME = 'handshake.json'

class PyLGTVPairException(Exception):
    def __init__(self, id, message):
        self.id = id
        self.message = message
        
class PyLGTVCmdException(Exception):
    pass


class WebOsClient(object):
    def __init__(self, ip, key_file_path=None, timeout_connect=2):
        """Initialize the client."""
        self.ip = ip
        self.port = 3000
        self.key_file_path = key_file_path
        self.client_key = None
        self.web_socket = None
        self.command_count = 0
        self.timeout_connect = timeout_connect
        self.connection = None
        self.input_connection = None
        self.listen_task = None
        self.listen_input_task = None
        self.callbacks = {}
        self.futures = {}

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

    async def connect(self):
        """Connect to tv and register."""
        
        #cleanup any existing connection first
        await self.disconnect()
        
        file = os.path.join(os.path.dirname(__file__), HANDSHAKE_FILE_NAME)

        data = codecs.open(file, 'r', 'utf-8')
        raw_handshake = data.read()

        handshake = json.loads(raw_handshake)
        handshake['payload']['client-key'] = self.client_key
        
        try:
            self.connection = await asyncio.wait_for(websockets.connect(f"ws://{self.ip}:{self.port}", ping_interval=1, ping_timeout=self.timeout_connect, close_timeout=self.timeout_connect), timeout=self.timeout_connect)

            await self.connection.send(json.dumps(handshake))
            raw_response = await self.connection.recv()
            response = json.loads(raw_response)

            if response['type'] == 'response' and \
                            response['payload']['pairingType'] == 'PROMPT':
                raw_response = await self.connection.recv()
                response = json.loads(raw_response)
                if response['type'] == 'registered':
                    self.client_key = response['payload']['client-key']
                    self.save_key_file()
                
            if not self.client_key:
                raise PyLGTVPairException("Unable to pair")
                
            self.callbacks = {}
            self.futures = {}
            self.listen_task = asyncio.get_event_loop().create_task(self.listen())

            #open additional connection needed to send button commands
            #the url is dynamically generated and returned from the EP_INPUT_SOCKET
            #endpoint on the main connection
            res = await self.request(EP_INPUT_SOCKET)
            inputsockpath = res.get("socketPath")
            self.input_connection =  await asyncio.wait_for(websockets.connect(inputsockpath, ping_interval=1, ping_timeout=self.timeout_connect, close_timeout=self.timeout_connect), timeout = self.timeout_connect)

            self.listen_input_task = asyncio.get_event_loop().create_task(self.listen_input())
        except:
            await self.disconnect()
            raise
            

    async def disconnect(self):
        """Stop listening and close connections."""
        
        if self.listen_task is not None:
            if not self.listen_task.done():
                self.listen_task.cancel()
        
        if self.listen_input_task is not None:
            if not self.listen_input_task.done():
                self.listen_input_task.cancel()
        
        self.callbacks = {}    

        for future in self.futures.values():
            future.set_exception(asyncio.CancelledError)
        self.futures = {}

        closeout = set()
        if self.listen_task is not None:
            closeout.add(self.listen_task)
        if self.listen_input_task is not None:
            closeout.add(self.listen_input_task)
        if self.connection is not None:
            closeout.add(self.connection.close())
        if self.input_connection is not None:
            closeout.add(self.input_connection.close())
        
        if closeout:
            await asyncio.wait(closeout)
            
        self.listen_task = None
        self.listen_input_task = None
        self.connection = None
        self.input_connection = None
        
    def is_registered(self):
        """Paired with the tv."""
        return self.client_key is not None
    
    def is_connected(self):
        """Connected to the tv."""
        return self.connection is not None and self.input_connection is not None

    async def listen(self):
        """Listen for incoming messages and handle disconnect."""
        try:
            async for raw_msg in self.connection:
                msg = json.loads(raw_msg)
                uid = msg.get('id')
                if uid in self.callbacks:
                    await self.callbacks[uid](msg.get('payload'))
                if uid in self.futures:
                    self.futures[uid].set_result(msg.get('payload'))
        except websockets.exceptions.ConnectionClosedError:
            #shielding of disconnect call and handling of cancel needed
            #to avoid circular dependency
            try:
                await asyncio.shield(self.disconnect())
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            return
            
    async def listen_input(self):
        """Listen for incoming messages and handle disconnect."""
        try:
            async for raw_msg in self.input_connection:
                pass
        except websockets.exceptions.ConnectionClosedError:
            #shielding of disconnect call and handling of cancel needed
            #to avoid circular dependency
            try:
                await asyncio.shield(self.disconnect())
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            return

    async def command(self, request_type, uri, payload=None, uid=None):
        """Build and send a command."""
        if uid is None:
            uid = str(uuid4())

        if payload is None:
            payload = {}

        message = {
            'id': uid,
            'type': request_type,
            'uri': "ssap://{}".format(uri),
            'payload': payload,
        }
        
        if self.connection is None:
            raise PyLGTVCmdException()

        await self.connection.send(json.dumps(message))

    async def request(self, uri, payload=None):
        """Send a request and wait for response."""
        uid = str(uuid4())
        res = asyncio.Future()
        self.futures[uid] = res
        await self.command('request', uri, payload, uid)
        try:
            response = await asyncio.wait_for(res, timeout = self.timeout_connect)
        except asyncio.TimeoutError:
            del self.futures[uid]
            await self.disconnect()
            raise
        except asyncio.CancelledError:
            raise
        del self.futures[uid]
        return response
    
    async def subscribe(self, callback, uri, payload=None):
        """Subscribe to updates."""
        uid = str(uuid4())
        self.callbacks[uid] = callback
        await self.command('subscribe', uri, payload, uid)
    
    async def button(self, name):
        """Send button press command."""
        
        message = f"type:button\nname:{name}\n\n"
        
        if self.input_connection is None:
            raise PyLGTVCmdException()
        
        await self.input_connection.send(message)
        
    async def move(self, dx, dy, down=0):
        """Send cursor move command."""
        
        message = f"type:move\ndx:{dx}\ndy:{dy}\ndown:{down}\n\n"
        
        if self.input_connection is None:
            raise PyLGTVCmdException()
        
        await self.input_connection.send(message)
        
    async def click(self):
        """Send cursor click command."""
        
        message = f"type:click\n\n"
        
        if self.input_connection is None:
            raise PyLGTVCmdException()
        
        await self.input_connection.send(message)
        
    async def scroll(self, dx, dy):
        """Send scroll command."""
        
        message = f"type:scroll\ndx:{dx}\ndy:{dy}\n\n"
        
        if self.input_connection is None:
            raise PyLGTVCmdException()
        
        await self.input_connection.send(message)

    async def send_message(self, message, icon_path=None):
        """Show a floating message."""
        icon_encoded_string = ''
        icon_extension = ''

        if icon_path is not None:
            icon_extension = os.path.splitext(icon_path)[1][1:]
            with open(icon_path, 'rb') as icon_file:
                icon_encoded_string = base64.b64encode(icon_file.read()).decode('ascii')

        await self.request(EP_SHOW_MESSAGE, {
            'message': message,
            'iconData': icon_encoded_string,
            'iconExtension': icon_extension
        })

    # Apps
    async def get_apps(self):
        """Return all apps."""
        res = await self.request(EP_GET_APPS)
        return res.get('launchPoints')
    
    async def subscribe_apps(self, callback):
        """Subscribe to changes in available apps."""
        
        async def apps(payload):
            await callback(payload.get('launchPoints'))
                           
        await self.subscribe(apps, EP_GET_APPS)

    async def get_current_app(self):
        """Get the current app id."""
        res = await self.request(EP_GET_CURRENT_APP_INFO)
        return res.get('appId')
    
    async def subscribe_current_app(self, callback):
        """Subscribe to changes in the current app id."""
        
        async def current_app(payload):
            await callback(payload.get('appId'))
        
        await self.subscribe(current_app, EP_GET_CURRENT_APP_INFO)

    async def launch_app(self, app):
        """Launch an app."""
        await self.request(EP_LAUNCH, {
            'id': app
        })

    async def launch_app_with_params(self, app, params):
        """Launch an app with parameters."""
        await self.request(EP_LAUNCH, {
            'id': app,
            'params': params
        })

    async def launch_app_with_content_id(self, app, contentId):
        """Launch an app with contentId."""
        await self.request(EP_LAUNCH, {
            'id': app,
            'contentId': contentId
        })

    async def close_app(self, app):
        """Close the current app."""
        await self.request(EP_LAUNCHER_CLOSE, {
            'id': app
        })

    # Services
    async def get_services(self):
        """Get all services."""
        res = await self.request(EP_GET_SERVICES)
        return res.get('services')

    async def get_software_info(self):
        """Return the current software status."""
        return await self.request(EP_GET_SOFTWARE_INFO)

    async def power_off(self):
        """Play media."""
        await self.request(EP_POWER_OFF)

    async def power_on(self):
        """Play media."""
        await self.request(EP_POWER_ON)

    # 3D Mode
    async def turn_3d_on(self):
        """Turn 3D on."""
        await self.request(EP_3D_ON)

    async def turn_3d_off(self):
        """Turn 3D off."""
        await self.request(EP_3D_OFF)

    # Inputs
    async def get_inputs(self):
        """Get all inputs."""
        res = await self.request(EP_GET_INPUTS)
        return res.get('devices')

    async def subscribe_inputs(self, callback):
        """Subscribe to changes in available inputs."""
        
        async def inputs(payload):
            await callback(payload.get('devices'))
                           
        await self.subscribe(inputs, EP_GET_INPUTS)

    async def get_input(self):
        """Get current input."""
        return await self.get_current_app()

    async def set_input(self, input):
        """Set the current input."""
        await self.request(EP_SET_INPUT, {
            'inputId': input
        })

    # Audio
    async def get_audio_status(self):
        """Get the current audio status"""
        return await self.request(EP_GET_AUDIO_STATUS)

    async def get_muted(self):
        """Get mute status."""
        status = await self.get_audio_status()
        return status.get('mute')
    
    async def subscribe_muted(self, callback):
        """Subscribe to changes in the current mute status."""
        
        async def muted(payload):
            await callback(payload.get('mute'))
        
        await self.subscribe(muted, EP_GET_AUDIO_STATUS)

    async def set_mute(self, mute):
        """Set mute."""
        await self.request(EP_SET_MUTE, {
            'mute': mute
        })

    async def get_volume(self):
        """Get the current volume."""
        res = await self.request(EP_GET_VOLUME)
        return res.get('volume')
    
    async def subscribe_volume(self, callback):
        """Subscribe to changes in the current volume."""
        
        async def volume(payload):
            await callback(payload.get('volume'))
        
        await self.subscribe(volume, EP_GET_VOLUME)

    async def set_volume(self, volume):
        """Set volume."""
        volume = max(0, volume)
        await self.request(EP_SET_VOLUME, {
            'volume': volume
        })

    async def volume_up(self):
        """Volume up."""
        await self.request(EP_VOLUME_UP)

    async def volume_down(self):
        """Volume down."""
        await self.request(EP_VOLUME_DOWN)

    # TV Channel
    async def channel_up(self):
        """Channel up."""
        await self.request(EP_TV_CHANNEL_UP)

    async def channel_down(self):
        """Channel down."""
        await self.request(EP_TV_CHANNEL_DOWN)

    async def get_channels(self):
        """Get all tv channels."""
        res = await self.request(EP_GET_TV_CHANNELS)
        return res.get('channelList')

    async def get_current_channel(self):
        """Get the current tv channel."""
        return await self.request(EP_GET_CURRENT_CHANNEL)
    
    async def subscribe_current_channel(self, callback):
        """Subscribe to changes in the current tv channel."""
        await self.subscribe(callback, EP_GET_CURRENT_CHANNEL)

    async def get_channel_info(self):
        """Get the current channel info."""
        return await self.request(EP_GET_CHANNEL_INFO)

    async def set_channel(self, channel):
        """Set the current channel."""
        await self.request(EP_SET_CHANNEL, {
            'channelId': channel
        })

    # Media control
    async def play(self):
        """Play media."""
        await self.request(EP_MEDIA_PLAY)

    async def pause(self):
        """Pause media."""
        await self.request(EP_MEDIA_PAUSE)

    async def stop(self):
        """Stop media."""
        await self.request(EP_MEDIA_STOP)

    async def close(self):
        """Close media."""
        await self.request(EP_MEDIA_CLOSE)

    async def rewind(self):
        """Rewind media."""
        await self.request(EP_MEDIA_REWIND)

    async def fast_forward(self):
        """Fast Forward media."""
        await self.request(EP_MEDIA_FAST_FORWARD)

    # Keys
    async def send_enter_key(self):
        """Send enter key."""
        await self.request(EP_SEND_ENTER)

    async def send_delete_key(self):
        """Send delete key."""
        await self.request(EP_SEND_DELETE)

    # Web
    async def open_url(self, url):
        """Open URL."""
        await self.request(EP_OPEN, {
            'target': url
        })

    async def close_web(self):
        """Close web app."""
        await self.request(EP_CLOSE_WEB_APP)
    
    #Emulated button presses
    async def left_button(self):
        """left button press."""
        await self.button("LEFT")

    async def right_button(self):
        """right button press."""
        await self.button("RIGHT")
        
    async def down_button(self):
        """down button press."""
        await self.button("DOWN")
        
    async def up_button(self):
        """up button press."""
        await self.button("UP")
        
    async def home_button(self):
        """home button press."""
        await self.button("HOME")
        
    async def back_button(self):
        """back button press."""
        await self.button("BACK")
        
    async def ok_button(self):
        """ok button press."""
        await self.button("ENTER")
        
    async def dash_button(self):
        """dash button press."""
        await self.button("DASH")
        
    async def info_button(self):
        """info button press."""
        await self.button("INFO")
        
    async def asterisk_button(self):
        """asterisk button press."""
        await self.button("ASTERISK")
        
    async def cc_button(self):
        """cc button press."""
        await self.button("CC")
        
    async def exit_button(self):
        """exit button press."""
        await self.button("EXIT")
        
    async def mute_button(self):
        """mute button press."""
        await self.button("MUTE")
        
    async def red_button(self):
        """red button press."""
        await self.button("RED")
        
    async def green_button(self):
        """green button press."""
        await self.button("GREEN")
        
    async def blue_button(self):
        """blue button press."""
        await self.button("BLUE")
        
    async def volume_up_button(self):
        """volume up button press."""
        await self.button("VOLUMEUP")
        
    async def volume_down_button(self):
        """volume down button press."""
        await self.button("VOLUMEDOWN")
        
    async def channel_up_button(self):
        """channel up button press."""
        await self.button("CHANNELUP")
        
    async def channel_down_button(self):
        """channel down button press."""
        await self.button("CHANNELDOWN")

    async def number_button(self, num):
        """numeric button press."""
        if not (num>=0 and num<=9):
            raise ValueError
        
        await self.button(f"""{num}""")
        
