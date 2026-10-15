"""Independent-library S3-07 test: forward a write, then discard its HTTP response.

Usage (after loading local secrets):
  python s3-07-fault-proxy.py execute DOCUMENT_ID drop-write|drop-both [receipt|issue]
  python s3-07-fault-proxy.py recover DOCUMENT_ID OPERATION_ID
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from ktg_agent.ktg.client import KTGClient

UPSTREAM = "http://127.0.0.1:18081"


def login() -> str:
    body = json.dumps({"username": os.environ["APP_ADMIN_USERNAME"],
                       "password": os.environ["APP_ADMIN_PASSWORD"],
                       "code": "", "uuid": ""}).encode()
    request = Request(UPSTREAM + "/login", data=body,
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=10) as response:
        payload = json.load(response)
    if payload.get("code") != 200 or not payload.get("token"):
        raise RuntimeError("Acceptance login failed")
    return "Bearer " + payload["token"]


class FaultProxy(BaseHTTPRequestHandler):
    drop_write = False
    drop_query = False
    write_count = 0
    query_count = 0

    def do_POST(self) -> None:
        self.forward()

    def do_GET(self) -> None:
        self.forward()

    def forward(self) -> None:
        if self.path != "/mes/wm/inventory-operations" and not self.path.startswith(
                "/mes/wm/inventory-operations/"):
            self.send_error(404)
            return
        is_write = self.command == "POST"
        if is_write:
            FaultProxy.write_count += 1
        else:
            FaultProxy.query_count += 1
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else None
        headers = {"Authorization": self.headers.get("Authorization", ""),
                   "Content-Type": "application/json"}
        request = Request(UPSTREAM + self.path, data=body, headers=headers,
                          method=self.command)
        try:
            with urlopen(request, timeout=30) as response:
                status, data = response.status, response.read()
        except HTTPError as error:
            status, data = error.code, error.read()
        if (is_write and self.drop_write) or (not is_write and self.drop_query):
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


async def main() -> None:
    command = sys.argv[1]
    document_id = int(sys.argv[2])
    authorization = login()
    if command == "recover":
        operation_id = sys.argv[3]
        client = KTGClient(base_url=UPSTREAM, authorization=authorization)
        result = await client.inventory_operation_result(operation_id)
    else:
        fault_mode = sys.argv[3]
        if fault_mode not in {"drop-write", "drop-both"}:
            raise ValueError("Unknown fault mode")
        kind = sys.argv[4] if len(sys.argv) > 4 else "receipt"
        if kind not in {"receipt", "issue"}:
            raise ValueError("Unknown inventory action")
        action = "ITEM_RECEIPT_EXECUTE" if kind == "receipt" else "PRODUCTION_ISSUE_EXECUTE"
        resource = "itemrecpt" if kind == "receipt" else "issueheader"
        direct = KTGClient(base_url=UPSTREAM, authorization=authorization)
        preview = (await direct.get(
            f"/mes/wm/{resource}/{document_id}/execution-preview"))["data"]
        if not isinstance(preview, dict) or preview.get("documentStatus") != "APPROVED":
            raise RuntimeError("Receipt is not approved")
        FaultProxy.drop_write = True
        FaultProxy.drop_query = fault_mode == "drop-both"
        server = ThreadingHTTPServer(("127.0.0.1", 0), FaultProxy)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = KTGClient(base_url=f"http://127.0.0.1:{server.server_port}",
                               authorization=authorization)
            operation_id = str(uuid4())
            result = await client.execute_inventory_operation({
                "actionType": action, "documentId": document_id,
                "operationId": operation_id, "expectedStatus": "APPROVED",
                "expectedVersion": preview["documentVersion"],
            })
        finally:
            server.shutdown()
            server.server_close()
        print(f"WRITE_COUNT={FaultProxy.write_count}")
        print(f"QUERY_COUNT={FaultProxy.query_count}")
    print(f"OPERATION_ID={operation_id}")
    print(f"STATUS={result.get('operationStatus')}")
    print(f"DOCUMENT_STATUS={result.get('documentStatus')}")
    print(f"TRANSACTION_IDS={','.join(str(row['transactionId']) for row in result.get('transactions', []))}")
    if result.get("safeToRetry") is not False:
        raise RuntimeError("Unsafe retry flag")


if __name__ == "__main__":
    asyncio.run(main())
