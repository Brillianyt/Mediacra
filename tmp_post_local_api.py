import json
import urllib.request
import urllib.error

req = urllib.request.Request(
    'http://127.0.0.1:18080/api/ai/activity/sync_structured_results',
    data=b'{"ids":[9]}',
    headers={'Content-Type': 'application/json'},
    method='POST',
)
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(resp.status)
        print(resp.read().decode('utf-8'))
except urllib.error.HTTPError as e:
    print(e.code)
    print(e.read().decode('utf-8'))
