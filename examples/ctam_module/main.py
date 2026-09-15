"""Minimal dependency-free CTAM API v1 module.

Copy this directory outside the checkout into ``ctam_modules/example_stats``
before enabling it.  The host supplies all connection details as environment
variables; modules never receive runtime filesystem paths.
"""
import json
import os
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE = os.environ["CTAM_API_URL"]
HEADERS = {"Authorization": f"Bearer {os.environ['CTAM_API_TOKEN']}", "X-CTAM-API-Version": "1", "Content-Type": "application/json"}

def request(path, *, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    with urlopen(Request(BASE + path, data=data, method=method, headers=HEADERS)) as response:
        return json.loads(response.read())["data"]

for cell in request("/stormcells")["cells"]:
    cell_id = quote(str(cell["id"]), safe="")
    current = request(f"/stormcells/{cell_id}")
    request(f"/stormcells/{cell_id}", method="PATCH", payload={"revision": current["revision"], "operations": [{"op": "add", "path": "/modules/ExampleStats", "value": {"status": "ok"}}]})

request("/routes/summary", method="PUT", payload={"status": "ok"})
request("/transaction/commit", method="POST", payload={})
