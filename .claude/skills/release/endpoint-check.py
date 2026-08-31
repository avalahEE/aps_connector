"""Call every read endpoint of the connector against one Odoo and report what breaks.

The module ships for three Odoo releases whose data models differ, and a column that
exists in 19 but not in 18 fails the whole endpoint - silently, because the sync only
logs a warning. Run this against every release before a version goes out.
"""
import json
import sys
import urllib.request

BASE = sys.argv[1]
KEY = sys.argv[2]

READ_ENDPOINTS = [
    'health', 'calendars', 'calendar_exceptions', 'maintenance_requests', 'resources',
    'products', 'boms', 'orders', 'operations', 'inventory', 'purchase_orders',
]

def call(path, payload):
    req = urllib.request.Request(
        f'{BASE}/aps/api/v1/{path}',
        data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': payload}).encode(),
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        body = json.loads(r.read())
    return body.get('result', body)

bad = 0
for ep in READ_ENDPOINTS:
    try:
        res = call(ep, {'api_key': KEY, 'apiKey': KEY, 'limit': 5})
    except Exception as e:
        print(f'  {ep:22} PÄRING KUKKUS: {e}')
        bad += 1
        continue

    if not isinstance(res, dict):
        print(f'  {ep:22} vastus pole objekt: {str(res)[:60]}')
        bad += 1
    elif res.get('error'):
        print(f'  {ep:22} VIGA: {res["error"]}')
        bad += 1
    elif res.get('warning'):
        print(f'  {ep:22} ok, hoiatus: {res["warning"]}')
    else:
        n = res.get('total', len(res.get('records', []) or res.get('bomLines', []) or []))
        print(f'  {ep:22} ok ({n})')

print(f'  --> katkiseid otspunkte: {bad}')
sys.exit(1 if bad else 0)
