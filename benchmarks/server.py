"""Bounded loopback receiver owned by the coordinator, never the measured worker.

The SDK has no public transport injection option. HTTP preserves its normal
loading, serialization, background-consumer and flush paths.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from fixtures import USERS, definitions


class BenchmarkServer:
    def __init__(self, spec, batch_delay=0):
        self.spec = spec
        self.lock = threading.Lock()
        self.payload = "x" * spec.get("payload_bytes", 0)
        self.body = json.dumps(definitions(spec.get("flag_count", 0))).encode()
        self.state: dict[str, Any] = {
            "definitions": 0,
            "delivered": 0,
            "requests": 0,
            "invalid_events": 0,
            "delivered_by_user": {user["distinct_id"]: 0 for user in USERS},
            "unexpected": [],
        }
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                with owner.lock:
                    path = urlsplit(self.path).path.rstrip("/")
                    if path == "/flags/definitions":
                        owner.state["definitions"] += 1
                        self.reply(200, owner.body)
                    elif path == "/__benchmark/state":
                        self.reply(200, json.dumps(owner.state).encode())
                    else:
                        self.unexpected(path)

            def unexpected(self, path):
                if len(owner.state["unexpected"]) < 20:
                    owner.state["unexpected"].append(path)
                self.reply(404, b'{"error":"unexpected benchmark request"}')

            def do_POST(self):
                with owner.lock:
                    path = urlsplit(self.path).path.rstrip("/")
                    try:
                        size = int(self.headers.get("Content-Length", "0"))
                        if not 0 < size <= 20 * 1024 * 1024:
                            raise ValueError("Invalid body length")
                        body = json.loads(self.rfile.read(size))
                        if path == "/__benchmark/definitions":
                            generation = body["generation"]
                            if (
                                not isinstance(generation, int)
                                or not 0 <= generation <= 1000
                            ):
                                raise ValueError("Invalid generation")
                            owner.body = json.dumps(
                                definitions(spec["flag_count"], generation)
                            ).encode()
                            self.reply(200, b"{}")
                        elif path == "/batch":
                            if batch_delay:
                                time.sleep(batch_delay)
                            batch = body["batch"]
                            if not isinstance(batch, list):
                                raise ValueError("Invalid batch")
                            owner.state["requests"] += 1
                            owner.state["delivered"] += len(batch)
                            for event in batch:
                                properties = event.get("properties", {})
                                user = event.get("distinct_id")
                                if (
                                    event.get("event") != "benchmark"
                                    or user not in owner.state["delivered_by_user"]
                                    or properties.get("payload") != owner.payload
                                    or properties.get("plan") != "pro"
                                ):
                                    owner.state["invalid_events"] += 1
                                else:
                                    owner.state["delivered_by_user"][user] += 1
                            self.reply(200, b'{"status":1}')
                        else:
                            self.unexpected(path)
                    except (ValueError, KeyError, TypeError, AttributeError):
                        owner.state["invalid_events"] += 1
                        self.reply(400, b'{"error":"invalid benchmark body"}')

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.url = f"http://127.0.0.1:{self.http.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
