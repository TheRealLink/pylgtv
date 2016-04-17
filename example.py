from pylgtv import WebOsClient

webos_client = WebOsClient('192.168.0.112', 3000)
#webos_client.launch_app('netflix')

for app in webos_client.get_apps():
    print(app)