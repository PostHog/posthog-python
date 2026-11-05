import json
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen

from fixtures import CASES, USERS, expected_flags, expected_value


def control(host, path, data=None):
    body = None if data is None else json.dumps(data).encode()
    request = Request(
        host + path, data=body, headers={"Content-Type": "application/json"}
    )
    with urlopen(request, timeout=5) as response:
        return json.load(response)


class Workload:
    def __init__(self, spec, host, client_class=None):
        if client_class is None:
            from posthog import Posthog

            client_class = Posthog
        self.spec = spec
        self.host = host
        self.generation = 0
        self.captured = 0
        self.captured_by_user = {user["distinct_id"]: 0 for user in USERS}
        self.errors: list[str] = []
        self.checks = 0
        self.flags = spec["mode"] in ("single", "all", "refresh")
        self.client = client_class(
            "benchmark-project-key",
            host=host,
            max_queue_size=100001,
            flush_at=100000,
            flush_interval=3600,
            poll_interval=3600,
            timeout=5,
            max_retries=0,
            feature_flags_request_max_retries=0,
            gzip=False,
            capture_mode="v0",
            thread=1,
            on_error=lambda error, _batch: self.errors.append(str(error)),
            **({"personal_api_key": "benchmark-local-only"} if self.flags else {}),
        )
        self.pool = (
            ThreadPoolExecutor(max_workers=spec["concurrency"])
            if spec["concurrency"] > 1
            else None
        )
        try:
            if self.flags:
                self.check_flags()
                assert self.state()["definitions"] > 0
                self.verify()
            else:
                self.prepare_phase(spec["operations"])
        except BaseException:
            self.close()
            raise

    def prepare_phase(self, operations):
        """Reset only outside measurement, after the preceding phase is verified."""
        if self.flags:
            self.generation = 0
            control(self.host, "/__benchmark/definitions", {"generation": 0})
            self.client.load_feature_flags()
            self.check_flags()
        else:
            control(self.host, "/__benchmark/reset", {"operations": operations})
            self.captured = 0
            self.captured_by_user = {user["distinct_id"]: 0 for user in USERS}
            self.events = [
                {
                    "event": "benchmark",
                    "distinct_id": USERS[i % len(USERS)]["distinct_id"],
                    "properties": {
                        "payload": "x" * self.spec["payload_bytes"],
                        "plan": "pro",
                        "sequence": i,
                    },
                }
                for i in range(operations)
            ]

    def state(self):
        return control(self.host, "/__benchmark/state")

    def evaluate(self, index):
        user = USERS[index % len(USERS)]
        options = {
            "person_properties": user["person_properties"],
            "only_evaluate_locally": True,
        }
        if self.spec["mode"] == "single":
            i = self.spec["flag_index"]
            return self.client.get_feature_flag(
                f"{CASES[i]}-{i}",
                user["distinct_id"],
                send_feature_flag_events=False,
                **options,
            )
        return self.client.get_all_flags(user["distinct_id"], **options)

    def check_flags(self):
        for i, user in enumerate(USERS):
            expected = (
                expected_value(self.spec["flag_index"], user, self.generation)
                if self.spec["mode"] == "single"
                else expected_flags(self.spec["flag_count"], user, self.generation)
            )
            actual = self.evaluate(i)
            assert actual == expected, (
                f"Wrong local evaluation for {self.spec['id']}, user {i}, generation {self.generation}"
            )
            self.checks += 1

    def run(self, operations=None, checkpoint=None):
        operations = self.spec["operations"] if operations is None else operations
        mode = self.spec["mode"]
        if mode == "refresh":
            for i in range(operations):
                self.generation += 1
                control(
                    self.host,
                    "/__benchmark/definitions",
                    {"generation": self.generation},
                )
                before = self.state()["definitions"]
                self.client.load_feature_flags()
                assert self.state()["definitions"] == before + 1
                if checkpoint:
                    checkpoint(i + 1)
        elif mode in ("capture", "drain"):
            for i in range(operations):
                event = self.events[i]
                assert self.client.capture(**event) is not None, (
                    "Capture rejected an event"
                )
                self.captured += 1
                self.captured_by_user[USERS[i % len(USERS)]["distinct_id"]] += 1
                completed = i + 1
                if (
                    mode == "drain"
                    and self.spec["drain_strategy"] == "steady"
                    and (
                        completed % self.spec["flush_every"] == 0
                        or completed == operations
                    )
                ):
                    self.client.flush(timeout_seconds=None)
                if checkpoint and (completed % 100 == 0 or completed == operations):
                    checkpoint(completed)
            if mode == "drain" and self.spec["drain_strategy"] == "burst":
                self.client.flush(timeout_seconds=None)
        else:
            for start in range(0, operations, self.spec["concurrency"]):
                if self.pool:
                    # Bound pending work and discard completed results; this is GIL-constrained threading.
                    futures = [
                        self.pool.submit(self.evaluate, i)
                        for i in range(
                            start, min(start + self.spec["concurrency"], operations)
                        )
                    ]
                    for future in futures:
                        future.result()
                else:
                    self.evaluate(start)
                completed = min(start + self.spec["concurrency"], operations)
                if checkpoint and (completed % 32 == 0 or completed == operations):
                    checkpoint(completed)

    def verify(self):
        if self.flags:
            self.check_flags()
        elif self.spec["mode"] == "capture":
            self.client.flush(timeout_seconds=5)
        state = self.state()
        assert not self.errors, self.errors
        assert not state["unexpected"], state["unexpected"]
        assert state["delivered"] == self.captured, (
            "Delivered counts must match captures"
        )
        assert state["invalid_events"] == 0, (
            "Capture payload, identity or sequence changed"
        )
        assert state["unique_sequences"] == self.captured
        assert state["missing_sequences"] == 0, "Missing capture sequences"
        assert state["delivered_by_user"] == self.captured_by_user, (
            "Capture identity distribution changed"
        )
        return {**state, "captured": self.captured, "evaluations": self.checks}

    def close(self):
        if self.pool:
            self.pool.shutdown(wait=True)
        self.client.shutdown()
