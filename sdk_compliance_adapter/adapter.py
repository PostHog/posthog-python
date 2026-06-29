"""
PostHog Python SDK Test Adapter

This adapter implements the SDK Test Adapter Interface defined in the PostHog Capture API Contract.
It wraps the posthog-python SDK and exposes a REST API for the test harness to exercise.
"""

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request

from posthog import Client
from posthog.capture_compression import CaptureCompression
from posthog.capture_v1 import post_v1 as original_post_v1
from posthog.request import EVENTS_ENDPOINT
from posthog.request import batch_post as original_batch_post
from posthog.version import VERSION

# Configure logging
logging.basicConfig(
    level=logging.WARNING, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Selects which capture protocol this adapter process speaks. Baked at build
# time via the CAPTURE_MODE env var ("v1" => capture-v1, anything else => legacy
# v0), mirroring the v0/v1 Dockerfile split. One process speaks one mode and
# advertises it via /health capabilities.
CAPTURE_MODE = os.environ.get("CAPTURE_MODE", "")


def is_v1() -> bool:
    return CAPTURE_MODE == "v1"


class RequestInfo:
    """Information about an HTTP request made by the SDK"""

    def __init__(
        self,
        timestamp_ms: int,
        status_code: int,
        retry_attempt: int,
        event_count: int,
        uuid_list: List[str],
    ):
        self.timestamp_ms = timestamp_ms
        self.status_code = status_code
        self.retry_attempt = retry_attempt
        self.event_count = event_count
        self.uuid_list = uuid_list

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp_ms": self.timestamp_ms,
            "status_code": self.status_code,
            "retry_attempt": self.retry_attempt,
            "event_count": self.event_count,
            "uuid_list": self.uuid_list,
        }


class SDKState:
    """Tracks SDK internal state for test assertions"""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending_events = 0
        self.total_events_captured = 0
        self.total_events_sent = 0
        self.total_retries = 0
        self.last_error: Optional[str] = None
        self.requests_made: List[RequestInfo] = []
        self.client: Optional[Client] = None
        self.retry_attempts: Dict[str, int] = {}  # Track retry attempts by batch ID

    def reset(self):
        """Reset all state"""
        client_to_shutdown = None
        with self.lock:
            client_to_shutdown = self.client
            self.client = None

        if client_to_shutdown:
            # Flush and shutdown the existing client outside state.lock.
            # The patched transport records successful flush requests through
            # SDKState.record_request(), which also needs state.lock. Holding the
            # lock while shutdown() waits for the queue to drain can deadlock when
            # a pending background event is being flushed during test reset.
            try:
                client_to_shutdown.shutdown()
            except Exception as e:
                logger.warning(f"Error shutting down client: {e}")

        with self.lock:
            self.pending_events = 0
            self.total_events_captured = 0
            self.total_events_sent = 0
            self.total_retries = 0
            self.last_error = None
            self.requests_made = []
            self.retry_attempts = {}

    def increment_captured(self):
        """Increment total events captured"""
        with self.lock:
            self.total_events_captured += 1
            self.pending_events += 1

    def record_request(self, status_code: int, batch: List[Dict], batch_id: str):
        """Record an HTTP request made by the SDK"""
        with self.lock:
            # Determine retry attempt for this batch
            retry_attempt = self.retry_attempts.get(batch_id, 0)

            # Extract UUIDs from batch
            uuid_list = [event.get("uuid", "") for event in batch]

            request_info = RequestInfo(
                timestamp_ms=int(time.time() * 1000),
                status_code=status_code,
                retry_attempt=retry_attempt,
                event_count=len(batch),
                uuid_list=uuid_list,
            )
            self.requests_made.append(request_info)

            # Update counters
            if status_code == 200:
                # Success - clear pending events
                self.total_events_sent += len(batch)
                self.pending_events = max(0, self.pending_events - len(batch))
                # Remove batch from retry tracking
                self.retry_attempts.pop(batch_id, None)
            else:
                # Failure - increment retry count
                self.retry_attempts[batch_id] = retry_attempt + 1
                if retry_attempt > 0:
                    self.total_retries += 1

    def record_request_v1(
        self, status_code: int, batch: List[Dict], attempt: int, terminal_count: int
    ):
        """Record a capture-v1 HTTP attempt.

        Unlike v0, the retry attempt is carried on the request (PostHog-Attempt,
        1-based) rather than inferred from a batch id, and a 2xx no longer means
        the whole batch was accepted — only events with a terminal (non-"retry")
        per-event result count as sent.
        """
        with self.lock:
            uuid_list = [event.get("uuid", "") for event in batch]
            self.requests_made.append(
                RequestInfo(
                    timestamp_ms=int(time.time() * 1000),
                    status_code=status_code,
                    retry_attempt=attempt - 1,
                    event_count=len(batch),
                    uuid_list=uuid_list,
                )
            )
            if attempt > 1:
                self.total_retries += 1
            if 200 <= status_code < 300:
                self.total_events_sent += terminal_count
                self.pending_events = max(0, self.pending_events - terminal_count)

    def record_error(self, error: str):
        """Record an error"""
        with self.lock:
            self.last_error = error

    def get_state(self) -> Dict[str, Any]:
        """Get current state as dict"""
        with self.lock:
            return {
                "pending_events": self.pending_events,
                "total_events_captured": self.total_events_captured,
                "total_events_sent": self.total_events_sent,
                "total_retries": self.total_retries,
                "last_error": self.last_error,
                "requests_made": [r.to_dict() for r in self.requests_made],
            }


# Global state
state = SDKState()


def create_batch_id(batch: List[Dict]) -> str:
    """Create a unique ID for a batch based on UUIDs"""
    uuids = sorted([event.get("uuid", "") for event in batch])
    return "-".join(uuids[:3])  # Use first 3 UUIDs as batch ID


def patched_batch_post(
    api_key: str,
    host: Optional[str] = None,
    gzip: bool = False,
    timeout: int = 15,
    path: str = EVENTS_ENDPOINT,
    **kwargs,
):
    """Patched version of batch_post that tracks requests"""
    batch = kwargs.get("batch", [])
    batch_id = create_batch_id(batch)

    try:
        # Call original batch_post
        response = original_batch_post(api_key, host, gzip, timeout, path, **kwargs)
        # Record successful request
        state.record_request(200, batch, batch_id)
        return response
    except Exception as e:
        # Record failed request
        status_code = (
            getattr(e, "status_code", 500) if hasattr(e, "status_code") else 500
        )
        state.record_request(status_code, batch, batch_id)
        state.record_error(str(e))
        raise


def patched_post_v1(
    api_key: str,
    host: Optional[str],
    batch_body: Dict,
    *,
    attempt: int,
    request_id: str,
    compression: CaptureCompression = CaptureCompression.NONE,
    timeout: int = 15,
    session: Any = None,
):
    """Patched version of post_v1 that records requests for /state assertions.

    Mirrors the legacy `patched_batch_post`, but reads the retry attempt from the
    call (1-based) and counts only terminal per-event results as sent.
    """
    batch = batch_body.get("batch", [])
    try:
        response = original_post_v1(
            api_key,
            host,
            batch_body,
            attempt=attempt,
            request_id=request_id,
            compression=compression,
            timeout=timeout,
            session=session,
        )
    except Exception as e:
        status_code = getattr(e, "status", 0)
        state.record_request_v1(
            status_code if isinstance(status_code, int) else 0, batch, attempt, 0
        )
        state.record_error(str(e))
        raise

    terminal = 0
    status = response.status_code
    if 200 <= status < 300:
        try:
            results = response.json().get("results", {})
            # Mirror send_v1_batch: only a non-retry directive is terminal. A
            # missing/null `result` is not counted as sent.
            terminal = sum(
                1
                for r in results.values()
                if (r or {}).get("result") not in (None, "retry")
            )
        except Exception:
            terminal = 0
    state.record_request_v1(status, batch, attempt, terminal)
    return response


# Monkey-patch the batch_post function
import posthog.request  # noqa: E402

posthog.request.batch_post = patched_batch_post

# Also patch in consumer module
import posthog.consumer  # noqa: E402

posthog.consumer.batch_post = patched_batch_post

# Patch the capture-v1 submitter. `send_v1_batch` resolves `post_v1` as a module
# global at call time, so patching it here covers both the async consumer and the
# sync client paths.
import posthog.capture_v1  # noqa: E402

posthog.capture_v1.post_v1 = patched_post_v1


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint"""
    capabilities = (
        ["capture_v1", "encoding_gzip"] if is_v1() else ["capture_v0", "encoding_gzip"]
    )
    return jsonify(
        {
            "sdk_name": "posthog-python",
            "sdk_version": VERSION,
            "adapter_version": "1.0.0",
            "capabilities": capabilities,
        }
    )


@app.route("/init", methods=["POST"])
def init():
    """Initialize the SDK client"""
    try:
        data = request.json or {}

        # Reset state
        state.reset()

        # Extract config
        api_key = data.get("api_key")
        host = data.get("host")
        flush_at = data.get("flush_at", 100)
        flush_interval_ms = data.get("flush_interval_ms", 500)
        max_retries = data.get("max_retries", 3)
        enable_compression = data.get("enable_compression", False)
        # Compliance tests assert the request-level default when callers omit
        # disable_geoip, so the adapter default keeps geoip-enabled /flags
        # requests while still allowing per-call overrides.
        disable_geoip = data.get("disable_geoip", False)
        historical_migration = data.get("historical_migration", False)

        if not api_key:
            return jsonify({"error": "api_key is required"}), 400
        if not host:
            return jsonify({"error": "host is required"}), 400

        # Convert flush_interval from ms to seconds
        flush_interval = flush_interval_ms / 1000.0

        # One adapter process speaks one capture protocol, selected by CAPTURE_MODE.
        capture_mode = "v1" if is_v1() else "v0"

        # Create client
        client = Client(
            project_api_key=api_key,
            host=host,
            flush_at=flush_at,
            flush_interval=flush_interval,
            gzip=enable_compression,
            max_retries=max_retries,
            debug=False,
            disable_geoip=disable_geoip,
            historical_migration=historical_migration,
            capture_mode=capture_mode,
        )

        state.client = client

        logger.info(
            f"Initialized SDK with api_key={api_key[:10]}..., host={host}, "
            f"flush_at={flush_at}, flush_interval={flush_interval}, "
            f"max_retries={max_retries}, gzip={enable_compression}, "
            f"capture_mode={capture_mode}, disable_geoip={disable_geoip}, "
            f"historical_migration={historical_migration}"
        )

        return jsonify({"success": True})
    except Exception as e:
        logger.exception("Error initializing SDK")
        return jsonify({"error": str(e)}), 500


@app.route("/capture", methods=["POST"])
def capture():
    """Capture a single event"""
    try:
        if not state.client:
            return jsonify({"error": "SDK not initialized"}), 400

        data = request.json or {}

        distinct_id = data.get("distinct_id")
        event = data.get("event")
        properties = data.get("properties")
        timestamp = data.get("timestamp")
        options = data.get("options")

        if not distinct_id:
            return jsonify({"error": "distinct_id is required"}), 400
        if not event:
            return jsonify({"error": "event is required"}), 400

        # Fold capture-v1 options back into the magic `$`-prefixed properties the
        # SDK lifts onto the wire `options` object. Renamed keys mirror the SDK's
        # sentinel table; unknown keys get a bare `$` prefix. v0 has no wire
        # options object, so this only applies in v1 mode.
        if options and is_v1():
            properties = dict(properties or {})
            option_to_property = {
                "cookieless_mode": "$cookieless_mode",
                "disable_skew_correction": "$ignore_sent_at",
                "process_person_profile": "$process_person_profile",
                "product_tour_id": "$product_tour_id",
            }
            for key, value in options.items():
                properties[option_to_property.get(key, "$" + key)] = value

        # Capture event
        kwargs = {"distinct_id": distinct_id, "properties": properties}
        if timestamp:
            # Parse ISO8601 timestamp
            from dateutil.parser import parse

            kwargs["timestamp"] = parse(timestamp)

        uuid = state.client.capture(event, **kwargs)

        # Track that we captured an event
        state.increment_captured()

        logger.info(f"Captured event: {event} for {distinct_id}, uuid={uuid}")

        return jsonify({"success": True, "uuid": uuid})
    except Exception as e:
        logger.exception("Error capturing event")
        state.record_error(str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/identify", methods=["POST"])
def identify():
    """Identify a user"""
    try:
        if not state.client:
            return jsonify({"error": "SDK not initialized"}), 400

        data = request.json or {}

        distinct_id = data.get("distinct_id")
        properties = data.get("properties")
        properties_set_once = data.get("properties_set_once")

        if not distinct_id:
            return jsonify({"error": "distinct_id is required"}), 400

        # Use the identify pattern - set + set_once
        if properties:
            state.client.set(distinct_id=distinct_id, properties=properties)
            state.increment_captured()

        if properties_set_once:
            state.client.set_once(
                distinct_id=distinct_id, properties=properties_set_once
            )
            state.increment_captured()

        logger.info(f"Identified user: {distinct_id}")

        return jsonify({"success": True})
    except Exception as e:
        logger.exception("Error identifying user")
        state.record_error(str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/flush", methods=["POST"])
def flush():
    """Force flush all pending events"""
    try:
        if not state.client:
            return jsonify({"error": "SDK not initialized"}), 400

        # Flush and wait
        state.client.flush()

        # Wait a bit for flush to complete
        # The flush() method triggers queue.join() which blocks until all items are processed
        time.sleep(0.5)

        logger.info("Flushed pending events")

        return jsonify({"success": True, "events_flushed": state.total_events_sent})
    except Exception as e:
        logger.exception("Error flushing events")
        state.record_error(str(e))
        return jsonify({"error": str(e), "errors": [str(e)]}, 500)


@app.route("/state", methods=["GET"])
def get_state():
    """Get internal SDK state"""
    try:
        return jsonify(state.get_state())
    except Exception as e:
        logger.exception("Error getting state")
        return jsonify({"error": str(e)}), 500


@app.route("/get_feature_flag", methods=["POST"])
def get_feature_flag():
    """Evaluate a feature flag"""
    try:
        if not state.client:
            return jsonify({"error": "SDK not initialized"}), 400

        data = request.json or {}

        key = data.get("key")
        distinct_id = data.get("distinct_id")
        person_properties = data.get("person_properties")
        groups = data.get("groups")
        group_properties = data.get("group_properties")
        disable_geoip = data.get("disable_geoip")
        force_remote = data.get("force_remote", True)

        if not key:
            return jsonify({"error": "key is required"}), 400
        if not distinct_id:
            return jsonify({"error": "distinct_id is required"}), 400

        value = state.client.get_feature_flag(
            key,
            distinct_id,
            person_properties=person_properties,
            groups=groups,
            group_properties=group_properties,
            disable_geoip=disable_geoip,
            only_evaluate_locally=not force_remote,
        )

        # Ensure the SDK's side-effect $feature_flag_called event is sent before
        # the adapter action returns. Otherwise the harness may reset mock-server
        # state for the next test while the background consumer is still flushing,
        # leaking the previous test's event into the next test.
        state.client.flush()

        logger.info(f"Feature flag {key} for {distinct_id}: {value}")

        return jsonify({"success": True, "value": value})
    except Exception as e:
        logger.exception("Error evaluating feature flag")
        return jsonify({"error": str(e)}), 500


@app.route("/reset", methods=["POST"])
def reset():
    """Reset SDK state"""
    try:
        state.reset()
        logger.info("Reset SDK state")
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("Error resetting state")
        return jsonify({"error": str(e)}), 500


def main():
    """Main entry point"""
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Starting SDK Test Adapter on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
