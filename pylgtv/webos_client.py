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
    def __init__(self, ip, key_file_path=None, timeout_connect=2, timeout_request=10, ping_interval=20, disconnect_callback = None):
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
        self.connect_task = None
        self.futures = {}
        self.disconnect_callback = disconnect_callback
        self.queue = asyncio.Queue()
        self.ping_queue = asyncio.Queue()
        self.input_queue = asyncio.Queue()
        self.input_ping_queue = asyncio.Queue()

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
        if self.is_connected():
            return True
        
        res = asyncio.Future()
        self.connect_task = asyncio.create_task(self.connect_handler(res))
        return await res
        
    async def disconnect(self):
        if self.connect_task is not None:
            if not self.connect_task.done():
                self.connect_task.cancel()
                await self.connect_task
            self.connect_task = None

    def registration_msg(self):
        handshake = copy.deepcopy(REGISTRATION_MESSAGE)
        handshake['payload']['client-key'] = self.client_key
        return handshake
    

    async def connect_handler(self, res):

        handler_tasks = set()
        try:
            async with WebSocketConnectWrapper(f"ws://{self.ip}:{self.port}",
                                               ping_interval=self.ping_interval,
                                               ping_timeout=self.timeout_connect,
                                               close_timeout=self.timeout_connect,
                                               connect_timeout=self.timeout_connect,
                                               disconnect_callback = None) as ws:
                await ws.send(json.dumps(self.registration_msg()))
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
                self.queue = asyncio.Queue()
                self.ping_queue = asyncio.Queue()
                
                handler_task = asyncio.create_task(self.handler(ws,self.callbacks,self.futures,self.queue,self.ping_queue))
                handler_tasks.add(handler_task)

                #open additional connection needed to send button commands
                #the url is dynamically generated and returned from the EP_INPUT_SOCKET
                #endpoint on the main connection
                sockres = await self.request(EP_INPUT_SOCKET)
                inputsockpath = sockres.get("socketPath")
                async with WebSocketConnectWrapper(inputsockpath,
                                                ping_interval=self.ping_interval,
                                                ping_timeout=self.timeout_connect,
                                                close_timeout=self.timeout_connect,
                                                connect_timeout=self.timeout_connect,
                                                disconnect_callback = self.disconnect_callback) as inputws:
                    
                    self.input_queue = asyncio.Queue()
                    self.input_ping_queue = asyncio.Queue()
                    input_handler_task = asyncio.create_task(self.handler(inputws, queue=self.input_queue, ping_queue=self.input_ping_queue))
                    handler_tasks.add(input_handler_task)
                    
                    res.set_result(True)
                    
                    done, pending = await asyncio.wait(handler_tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.wait(pending)
        except Exception as ex:
            if not res.done():
                res.set_exception(ex)
        finally:
            for task in handler_tasks:
                if not task.done() and not task.cancelled():
                    task.cancel()
            
            if handler_tasks:
                await asyncio.wait(handler_tasks)
                

    async def handler(self, ws, callbacks={}, futures={}, queue=asyncio.Queue(), ping_queue=asyncio.Queue()):
        handlers = set()
        try:
            handlers.add(asyncio.create_task(self.consumer_handler(ws,callbacks,futures)))
            handlers.add(asyncio.create_task(self.producer_handler(ws,queue)))
            handlers.add(asyncio.create_task(self.ping_handler(ws,ping_queue)))
            
            done, pending = await asyncio.wait(handlers, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            
            if pending:
                await asyncio.wait(pending)
        finally:
            for task in handlers:
                if not task.done() and not task.cancelled():
                    task.cancel()
            if handlers:
                await asyncio.wait(handlers)

    async def ping_handler(self, ws, queue=asyncio.Queue()):
        try: 
            while True:
                res = await queue.get()
                try:
                    pong_waiter = await ws.ping()
                    try:
                        await asyncio.wait_for(pong_waiter, timeout = self.timeout_connect)
                    except asyncio.TimeoutError as ex:
                        pong_waiter.result()
                        res.set_exception(ex)
                        break
                    
                    res.set_result(True)
                except (websockets.exceptions.ConnectionClosedError) as ex:
                    res.set_exception(ex)
                    break
                finally:
                    if not res.done():
                        res.cancel()
                queue.task_done()
        except asyncio.CancelledError:
            pass
        finally:
            while not queue.empty():
                res = queue.get_nowait()
                res.cancel()
                queue.task_done()
                
            
    async def consumer_handler(self, ws, callbacks={}, futures={}):
        try:
            async for raw_msg in ws:
                if callbacks or futures:
                    msg = json.loads(raw_msg)
                    uid = msg.get('id')
                    if uid in self.callbacks:
                        await self.callbacks[uid](msg.get('payload'))
                    if uid in self.futures:
                        self.futures[uid].set_result(msg.get('payload'))
        except (websockets.exceptions.ConnectionClosedError, asyncio.CancelledError):
            pass
        finally:
            callbacks.clear()
            for future in futures.values():
                future.cancel()
            futures.clear()
        
    async def producer_handler(self, ws, queue):
        try: 
            while True:
                res,raw_msg = await queue.get()
                try:
                    await ws.send(raw_msg)
                    res.set_result(True)
                except websockets.exceptions.ConnectionClosedError as ex:
                    res.set_exception(ex)
                    break
                finally:
                    if not res.done():
                        res.cancel()
                queue.task_done()
        except asyncio.CancelledError:
            #try to send all messages currently in the queue
            sending = set()
            while not queue.empty():
                raw_msg = queue.get_nowait()
                sending.add(asyncio.create_task(ws.send(raw_msg)))
            if sending:
                try:
                    done,pending = await asyncio.wait(sending)
                except websockets.exceptions.ConnectionClosedError:
                    pass
                for item in done:
                    queue.task_done()
        finally:
            while not queue.empty():
                res, raw_msg = queue.get_nowait()
                res.cancel()
                queue.task_done()


    def is_registered(self):
        """Paired with the tv."""
        return self.client_key is not None
    
    def is_connected(self):
        if self.connect_task is None:
            return False
        return not (self.connect_task.done() or self.connect_task.cancelled())
    
  
    async def ping(self):
        res = asyncio.Future()
        inputres = asyncio.Future()
        
        await self.ping_queue.put(res)
        await self.input_ping_queue.put(inputres)
        
        
        await asyncio.wait({res,inputres})
        try:
            res.result()
        except (websockets.exceptions.ConnectionClosedError, asyncio.TimeoutError, asyncio.CancelledError):
            pass
        try:
            inputres.result()
        except (websockets.exceptions.ConnectionClosedError, asyncio.TimeoutError, asyncio.CancelledError):
            pass

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
        
        if not self.is_connected():
            raise PyLGTVCmdException()
        
        res = asyncio.Future()
        await self.queue.put((res, json.dumps(message)))
        await res

    async def request(self, uri, payload=None):
        """Send a request and wait for response."""
        uid = self.command_count
        self.command_count += 1
        res = asyncio.Future()
        self.futures[uid] = res
        try:
            await self.command('request', uri, payload, uid)
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
        await self.command('subscribe', uri, payload, uid)
    
    async def input_command(self, message):
        if not self.is_connected():
            raise PyLGTVCmdException()
        
        res = asyncio.Future()
        await self.input_queue.put((res, message))
        await res
    
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

    async def power_off(self, disconnect=True):
        """Power off TV."""
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
        
