import asyncio
import base64
import codecs
import json
import os
import websockets
import logging
import sys
import copy

logger = logging.getLogger(__name__)

from .endpoints import *
from .handshake import REGISTRATION_MESSAGE

KEY_FILE_NAME = '.pylgtv'
USER_HOME = 'HOME'
HANDSHAKE_FILE_NAME = 'handshake.json'

class PyLGTVPairException(Exception):
    def __init__(self, id, message):
        self.id = id
        self.message = message
        
class PyLGTVCmdException(Exception):
    pass

#simple wrapper to add timeout and disconnect callback to websocket connection async context manager
class WebSocketConnectWrapper(websockets.connect):
    def __init__(self, *args, **kwargs):
        self.connect_timeout = kwargs.pop('connect_timeout', None)
        self.disconnect_callback = kwargs.pop('disconnect_callback', None)
        super(WebSocketConnectWrapper, self).__init__(*args, **kwargs)
    
    async def __aenter__(self, *args, **kwargs):
        if self.connect_timeout is not None:
            return await asyncio.wait_for(super(WebSocketConnectWrapper, self).__aenter__(*args, **kwargs), timeout = self.connect_timeout)
        else:
            return await super(WebSocketConnectWrapper, self).__aenter__(*args, **kwargs)
    
    async def __aexit__(self, *args, **kwargs):
        if self.disconnect_callback is not None:
            _,res = await asyncio.gather(self.disconnect_callback(),
                                 super(WebSocketConnectWrapper, self).__aexit__(*args, **kwargs)
                                 )
            return res
        else:
            return await super(WebSocketConnectWrapper, self).__aexit__(*args, **kwargs)
    

class WebOsClient(object):
    def __init__(self, ip, key_file_path=None, timeout_connect=2, timeout_request=10, ping_interval=20, standby_connection = False):
        """Initialize the client."""
        self.ip = ip
        self.port = 3000
        self.key_file_path = key_file_path
        self.client_key = None
        self.web_socket = None
        self.command_count = 0
        self.timeout_connect = timeout_connect
        self.timeout_request = timeout_request
        self.ping_interval = ping_interval
        self.standby_connection = standby_connection
        self.connect_task = None
        self.connect_result = None
        self.connection = None
        self.input_connection = None
        self.callbacks = {}
        self.futures = {}
        self._current_appId = ""
        self._muted = muted = False
        self._volume = 0
        self._current_channel = None
        self._apps = {}
        self._extinputs = {}
        self.state_update_callbacks = []
        self.doStateUpdate = False

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
        if not self.is_connected():
            self.connect_result = asyncio.Future()
            self.connect_task = asyncio.create_task(self.connect_handler(self.connect_result))
        return await self.connect_result
        
    async def disconnect(self):
        if self.is_connected():
            self.connect_task.cancel()
            print("disconnect: disconnecting")
            await self.connect_task
            print("disconnect: disconnected")
        
    def is_registered(self):
        """Paired with the tv."""
        return self.client_key is not None
    
    def is_connected(self):
        return (self.connect_task is not None and not self.connect_task.done())

    def registration_msg(self):
        handshake = copy.deepcopy(REGISTRATION_MESSAGE)
        handshake['payload']['client-key'] = self.client_key
        return handshake

    async def connect_handler(self, res):

        handler_tasks = set()
        ws = None
        inputws = None
        try:
            print("trying to connect")
            ws = await asyncio.wait_for(websockets.connect(f"ws://{self.ip}:{self.port}",
                                                    ping_interval=None,
                                                    close_timeout=self.timeout_connect),
                                timeout = self.timeout_connect)
            print("sending reg msg")
            await ws.send(json.dumps(self.registration_msg()))
            print("awaiting response")
            raw_response = await ws.recv()
            response = json.loads(raw_response)

            if response['type'] == 'response' and \
                            response['payload']['pairingType'] == 'PROMPT':
                raw_response = await ws.recv()
                response = json.loads(raw_response)
                if response['type'] == 'registered':
                    self.client_key = response['payload']['client-key']
                    self.save_key_file()
                
            if not self.client_key:
                raise PyLGTVPairException("Unable to pair")
            
            self.callbacks = {}
            self.futures = {}
            
            print("starting listener")
            handler_tasks.add(asyncio.create_task(self.consumer_handler(ws,self.callbacks,self.futures)))
            if self.ping_interval is not None:
                handler_tasks.add(asyncio.create_task(self.ping_handler(ws, self.ping_interval)))
            self.connection = ws
            
            #open additional connection needed to send button commands
            #the url is dynamically generated and returned from the EP_INPUT_SOCKET
            #endpoint on the main connection
            sockres = await self.request(EP_INPUT_SOCKET)
            inputsockpath = sockres.get("socketPath")
            inputws = await asyncio.wait_for(websockets.connect(inputsockpath,
                                                        ping_interval=None,
                                                        close_timeout=self.timeout_connect),
                                    timeout = self.timeout_connect)            

            handler_tasks.add(asyncio.create_task(inputws.wait_closed()))
            if self.ping_interval is not None:
                handler_tasks.add(asyncio.create_task(self.ping_handler(inputws, self.ping_interval)))
            self.input_connection = inputws
            
            #subscribe to state updates
            #avoid partial updates during initial subscription
            
            self.doStateUpdate = False
            await asyncio.gather(self.subscribe_current_app(self.set_current_app_state),
                                 self.subscribe_muted(self.set_muted_state),
                                 self.subscribe_volume(self.set_volume_state),
                                 self.subscribe_current_channel(self.set_current_channel_state),
                                 self.subscribe_apps(self.set_apps_state),
                                 self.subscribe_inputs(self.set_inputs_state),
                                 )
            self.doStateUpdate = True
            if self.state_update_callbacks:
                await self.do_state_update_callbacks()
            
            res.set_result(True)
            
            await asyncio.wait(handler_tasks, return_when=asyncio.FIRST_COMPLETED)
            
        except Exception as ex:
            if not res.done():
                res.set_exception(ex)
        finally:
            print("disconnecting")
            for task in handler_tasks:
                if not task.done():
                    task.cancel()
                    
            for future in self.futures:
                future.cancel()
            
            closeout = set()
            closeout.update(handler_tasks)
            
            if ws is not None:
                closeout.add(asyncio.create_task(ws.close()))
            if inputws is not None:
                closeout.add(asyncio.create_task(inputws.close()))
            
            self.connection = None
            self.input_connection = None
            
            self._current_appId = ""
            self._muted = muted = False
            self._volume = 0
            self._current_channel = None
            self._apps = {}
            self._extinputs = {}
            
            self.doStateUpdate = True
            
            for callback in self.state_update_callbacks:
                closeout.add(callback())
            
            if closeout:
                print("closeout")
                closeout_task = asyncio.create_task(asyncio.wait(closeout))
                
                while not closeout_task.done():
                    try:
                        await asyncio.shield(closeout_task)
                    except asyncio.CancelledError:
                        pass
            print("disconnected")

    async def ping_handler(self, ws, interval=20):
        try:
            while True:
                await asyncio.sleep(interval)
                if self.current_appId != "" or not self.standby_connection:
                    print("pinging")
                    ping_waiter = await ws.ping()
                    await asyncio.wait_for(ping_waiter, timeout = self.timeout_connect)
        except (asyncio.TimeoutError, asyncio.CancelledError, websockets.exceptions.ConnectionClosedError):
            pass

    async def consumer_handler(self, ws, callbacks={}, futures={}):
        try:
            async for raw_msg in ws:
                if callbacks or futures:
                    msg = json.loads(raw_msg)
                    uid = msg.get('id')
                    payload = msg.get('payload')
                    if uid in self.callbacks:
                        await self.callbacks[uid](payload)
                    if uid in self.futures:
                        self.futures[uid].set_result(payload)
        except (websockets.exceptions.ConnectionClosedError, asyncio.CancelledError):
            pass

    #manage state
    @property
    def current_appId(self):
        return self._current_appId
    
    @property
    def muted(self):
        return self._muted
    
    @property
    def volume(self):
        return self._volume
    
    @property
    def current_channel(self):
        return self._current_channel
    
    @property
    def apps(self):
        return self._apps
    
    @property
    def inputs(self):
        return self._extinputs
    
    async def register_state_update_callback(self, callback):
        self.state_update_callbacks.append(callback)
        if self.doStateUpdate:
            await callback()
        
    def unregister_state_update_callback(self, callback):
        if callback in self.state_update_callbacks:
            self.state_update_callbacks.remove(callback)
            
    def clear_state_update_callbacks(self):
        self.state_update_callbacks = []
    
    async def do_state_update_callbacks(self):
        callbacks = set()
        for callback in self.state_update_callbacks:
            callbacks.add(callback())
            
        if callbacks:
            await asyncio.gather(*callbacks)
    
    async def set_current_app_state(self, appId):
        self._current_appId = appId
        
        if self.state_update_callbacks and self.doStateUpdate:
            await self.do_state_update_callbacks()
        
    async def set_muted_state(self, muted):
        self._muted = muted
        
        if self.state_update_callbacks and self.doStateUpdate:
            await self.do_state_update_callbacks()
        
    async def set_volume_state(self, volume):
        self._volume = volume
        
        if self.state_update_callbacks and self.doStateUpdate:
            await self.do_state_update_callbacks()
        
    async def set_current_channel_state(self, channel):
        self._current_channel = channel
        
        if self.state_update_callbacks and self.doStateUpdate:
            await self.do_state_update_callbacks()
        
    async def set_apps_state(self, apps):
        self._apps = {}
        for app in apps:
            self._apps[app["id"]] = app
            
        if self.state_update_callbacks and self.doStateUpdate:
            await self.do_state_update_callbacks()
        
    async def set_inputs_state(self, extinputs):
        self._extinputs = {}
        for extinput in extinputs:
            self._extinputs[extinput["appId"]] = extinput
            
        if self.state_update_callbacks and self.doStateUpdate:
            await self.do_state_update_callbacks()

    #low level request handling

    async def command(self, request_type, uri, payload=None, uid=None):
        """Build and send a command."""
        if uid is None:
            uid = self.command_count
            self.command_count += 1

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

    async def request(self, uri, payload=None, cmd_type='request', uid=None):
        """Send a request and wait for response."""
        if uid is None:
            uid = self.command_count
            self.command_count += 1
        res = asyncio.Future()
        self.futures[uid] = res
        try:
            await self.command(cmd_type, uri, payload, uid)
        except PyLGTVCmdException:
            del self.futures[uid]
            raise
        try:
            response = await asyncio.wait_for(res, timeout = self.timeout_request)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            if uid in self.futures:
                del self.futures[uid]
            raise
        del self.futures[uid]
        return response
    
    async def subscribe(self, callback, uri, payload=None):
        """Subscribe to updates."""
        uid = self.command_count
        self.command_count += 1
        self.callbacks[uid] = callback
        return await self.request(uri, payload=payload, cmd_type='subscribe', uid=uid)
    
    async def input_command(self, message):
        if self.input_connection is None:
            raise PyLGTVCmdException()
        
        await self.input_connection.send(message)
    
    #high level request handling
    
    async def button(self, name):
        """Send button press command."""
        
        message = f"type:button\nname:{name}\n\n"
        await self.input_command(message)
        
    async def move(self, dx, dy, down=0):
        """Send cursor move command."""
        
        message = f"type:move\ndx:{dx}\ndy:{dy}\ndown:{down}\n\n"
        await self.input_command(message)
        
    async def click(self):
        """Send cursor click command."""
        
        message = f"type:click\n\n"
        await self.input_command(message)
        
    async def scroll(self, dx, dy):
        """Send scroll command."""
        
        message = f"type:scroll\ndx:{dx}\ndy:{dy}\n\n"
        await self.input_command(message)

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
                           
        return await self.subscribe(apps, EP_GET_APPS)

    async def get_current_app(self):
        """Get the current app id."""
        res = await self.request(EP_GET_CURRENT_APP_INFO)
        return res.get('appId')
    
    async def subscribe_current_app(self, callback):
        """Subscribe to changes in the current app id."""
        
        async def current_app(payload):
            await callback(payload.get('appId'))
        
        return await self.subscribe(current_app, EP_GET_CURRENT_APP_INFO)

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

    async def power_off(self, disconnect=None):
        """Power off TV."""
        if disconnect is None:
            disconnect = not self.standby_connection

        if disconnect:
            #if tv is shutting down and standby++ option is not enabled,
            #response is unreliable, so don't wait for one,
            #and force immediate disconnect
            await self.command('request', EP_POWER_OFF)
            await self.disconnect()
        else:
            #if standby++ option is enabled, connection stays open
            #and TV responds gracefully to power off request
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
                           
        return await self.subscribe(inputs, EP_GET_INPUTS)

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
        
        return await self.subscribe(muted, EP_GET_AUDIO_STATUS)

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
        
        return await self.subscribe(volume, EP_GET_VOLUME)

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
        return await self.subscribe(callback, EP_GET_CURRENT_CHANNEL)

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
        
