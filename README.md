# pylgtv
Library to control webOS based LG Tv devices

## Requirements
- Python >= 3.3

## Install
```
pip install pylgtv
```

## Example

```python
from pylgtv import WebOsClient

import sys
import logging

logging.basicConfig(stream=sys.stdout, level=logging.INFO)

try:
    webos_client = WebOsClient('192.168.0.112')
    #webos_client.launch_app('netflix')

    for app in webos_client.get_apps():
        print(app)
except:
    print("Error connecting to TV")
```
