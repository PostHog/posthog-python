import atexit
import hashlib
import logging
import numbers
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from dateutil.tz import tzutc
from six import string_types

from posthog.consumer import Consumer
from posthog.poller import Poller
from posthog.request import APIError, batch_post, decide, get
from posthog.utils import clean, guess_timezone
from posthog.version import VERSION

try:
    import queue
except ImportError:
    import Queue as queue


ID_TYPES = (numbers.Number, string_types, UUID)
__LONG_SCALE__ = float(0xFFFFFFFFFFFFFFF)


class Client(object):
    """Create a new PostHog client."""

    log = logging.getLogger("posthog")

    def __init__(
        self,
        api_key=None,
        host=None,
        debug=False,
        max_queue_size=10000,
        send=True,
        on_error=None,
        flush_at=100,
        flush_interval=0.5,
        gzip=False,
        max_retries=3,
        sync_mode=False,
        timeout=15,
        thread=1,
        poll_interval=30,
        personal_api_key=None,
        project_api_key=None,
    ):

        self.queue = queue.Queue(max_queue_size)

        # api_key: This should be the Team API Key (token), public
        self.api_key = project_api_key or api_key

        require("api_key", self.api_key, string_types)

        self.on_error = on_error
        self.debug = debug
        self.send = send
        self.sync_mode = sync_mode
        self.host = host
        self.gzip = gzip
        self.timeout = timeout
        self.feature_flags = None
        self.poll_interval = poll_interval
        self.poller = None

        # personal_api_key: This should be a generated Personal API Key, private
        self.personal_api_key = personal_api_key

        if debug:
            self.log.setLevel(logging.DEBUG)

        if sync_mode:
            self.consumers = None
        else:
            # On program exit, allow the consumer thread to exit cleanly.
            # This prevents exceptions and a messy shutdown when the
            # interpreter is destroyed before the daemon thread finishes
            # execution. However, it is *not* the same as flushing the queue!
            # To guarantee all messages have been delivered, you'll still need
            # to call flush().
            if send:
                atexit.register(self.join)
            for n in range(thread):
                self.consumers = []
                consumer = Consumer(
                    self.queue,
                    self.api_key,
                    host=host,
                    on_error=on_error,
                    flush_at=flush_at,
                    flush_interval=flush_interval,
                    gzip=gzip,
                    retries=max_retries,
                    timeout=timeout,
                )
                self.consumers.append(consumer)

                # if we've disabled sending, just don't start the consumer
                if send:
                    consumer.start()

    def identify(self, distinct_id=None, properties=None, context=None, timestamp=None, message_id=None):
        properties = properties or {}
        context = context or {}
        require("distinct_id", distinct_id, ID_TYPES)
        require("properties", properties, dict)

        msg = {
            "timestamp": timestamp,
            "context": context,
            "distinct_id": distinct_id,
            "$set": properties,
            "event": "$identify",
            "messageId": message_id,
        }

        return self._enqueue(msg)

    def capture(
        self, distinct_id=None, event=None, properties=None, context=None, timestamp=None, message_id=None, groups=None
    ):
        properties = properties or {}
        context = context or {}
        require("distinct_id", distinct_id, ID_TYPES)
        require("properties", properties, dict)
        require("event", event, string_types)

        msg = {
            "properties": properties,
            "timestamp": timestamp,
            "context": context,
            "distinct_id": distinct_id,
            "event": event,
            "messageId": message_id,
        }

        if groups:
            require("groups", groups, dict)
            msg["properties"]["$groups"] = groups

        return self._enqueue(msg)

    def set(self, distinct_id=None, properties=None, context=None, timestamp=None, message_id=None):
        properties = properties or {}
        context = context or {}
        require("distinct_id", distinct_id, ID_TYPES)
        require("properties", properties, dict)

        msg = {
            "timestamp": timestamp,
            "context": context,
            "distinct_id": distinct_id,
            "$set": properties,
            "event": "$set",
            "messageId": message_id,
        }

        return self._enqueue(msg)

    def set_once(self, distinct_id=None, properties=None, context=None, timestamp=None, message_id=None):
        properties = properties or {}
        context = context or {}
        require("distinct_id", distinct_id, ID_TYPES)
        require("properties", properties, dict)

        msg = {
            "timestamp": timestamp,
            "context": context,
            "distinct_id": distinct_id,
            "$set_once": properties,
            "event": "$set_once",
            "messageId": message_id,
        }

        return self._enqueue(msg)

    def group_identify(
        self, group_type=None, group_key=None, properties=None, context=None, timestamp=None, message_id=None
    ):
        properties = properties or {}
        context = context or {}
        require("group_type", group_type, ID_TYPES)
        require("group_key", group_key, ID_TYPES)
        require("properties", properties, dict)

        msg = {
            "event": "$groupidentify",
            "properties": {
                "$group_type": group_type,
                "$group_key": group_key,
                "$group_set": properties,
            },
            "distinct_id": "${}_{}".format(group_type, group_key),
            "timestamp": timestamp,
            "context": context,
            "messageId": message_id,
        }

        return self._enqueue(msg)

    def alias(self, previous_id=None, distinct_id=None, context=None, timestamp=None, message_id=None):
        context = context or {}

        require("previous_id", previous_id, ID_TYPES)
        require("distinct_id", distinct_id, ID_TYPES)

        msg = {
            "properties": {
                "distinct_id": previous_id,
                "alias": distinct_id,
            },
            "timestamp": timestamp,
            "context": context,
            "event": "$create_alias",
            "distinct_id": previous_id,
        }

        return self._enqueue(msg)

    def page(self, distinct_id=None, url=None, properties=None, context=None, timestamp=None, message_id=None):
        properties = properties or {}
        context = context or {}

        require("distinct_id", distinct_id, ID_TYPES)
        require("properties", properties, dict)

        require("url", url, string_types)
        properties["$current_url"] = url

        msg = {
            "event": "$pageview",
            "properties": properties,
            "timestamp": timestamp,
            "context": context,
            "distinct_id": distinct_id,
            "messageId": message_id,
        }

        return self._enqueue(msg)

    def _enqueue(self, msg):
        """Push a new `msg` onto the queue, return `(success, msg)`"""
        timestamp = msg["timestamp"]
        if timestamp is None:
            timestamp = datetime.utcnow().replace(tzinfo=tzutc())
        message_id = msg.get("messageId")
        if message_id is None:
            message_id = uuid4()

        require("timestamp", timestamp, datetime)
        require("context", msg["context"], dict)

        # add common
        timestamp = guess_timezone(timestamp)
        msg["timestamp"] = timestamp.isoformat()
        msg["messageId"] = stringify_id(message_id)
        if not msg.get("properties"):
            msg["properties"] = {}
        msg["properties"]["$lib"] = "posthog-python"
        msg["properties"]["$lib_version"] = VERSION

        msg["distinct_id"] = stringify_id(msg.get("distinct_id", None))

        msg = clean(msg)
        self.log.debug("queueing: %s", msg)

        # if send is False, return msg as if it was successfully queued
        if not self.send:
            return True, msg

        if self.sync_mode:
            self.log.debug("enqueued with blocking %s.", msg["event"])
            batch_post(self.api_key, self.host, gzip=self.gzip, timeout=self.timeout, batch=[msg])

            return True, msg

        try:
            self.queue.put(msg, block=False)
            self.log.debug("enqueued %s.", msg["event"])
            return True, msg
        except queue.Full:
            self.log.warning("analytics-python queue is full")
            return False, msg

    def flush(self):
        """Forces a flush from the internal queue to the server"""
        queue = self.queue
        size = queue.qsize()
        queue.join()
        # Note that this message may not be precise, because of threading.
        self.log.debug("successfully flushed about %s items.", size)

    def join(self):
        """Ends the consumer thread once the queue is empty.
        Blocks execution until finished
        """
        for consumer in self.consumers:
            consumer.pause()
            try:
                consumer.join()
            except RuntimeError:
                # consumer thread has not started
                pass

        if self.poller:
            self.poller.stop()

    def shutdown(self):
        """Flush all messages and cleanly shutdown the client"""
        self.flush()
        self.join()

    def _load_feature_flags(self):
        try:
            flags = get(self.personal_api_key, f"/api/feature_flag/?token={self.api_key}", self.host)["results"]
            self.feature_flags = [flag for flag in flags if flag["active"]]
        except APIError as e:
            if e.status == 401:
                raise APIError(
                    status=401,
                    message="You are using a write-only key with feature flags. "
                    "To use feature flags, please set a personal_api_key "
                    "More information: https://posthog.com/docs/api/overview",
                )
            else:
                raise APIError(status=e.status, message=e.message)
        except Exception as e:
            self.log.warning(
                "[FEATURE FLAGS] Fetching feature flags failed with following error. We will retry in %s seconds."
                % self.poll_interval
            )
            self.log.warning(e)

        self._last_feature_flag_poll = datetime.utcnow().replace(tzinfo=tzutc())

    def load_feature_flags(self):
        if not self.personal_api_key:
            self.log.warning("[FEATURE FLAGS] You have to specify a personal_api_key to use feature flags.")
            self.feature_flags = []
            return

        self._load_feature_flags()
        if not (self.poller and self.poller.is_alive()):
            self.poller = Poller(interval=timedelta(seconds=self.poll_interval), execute=self._load_feature_flags)
            self.poller.start()

    def feature_enabled(self, key, distinct_id, default=False, *, groups={}):
        require("key", key, string_types)
        require("distinct_id", distinct_id, ID_TYPES)
        require("groups", groups, dict)

        if not self.personal_api_key:
            self.log.warning("[FEATURE FLAGS] You have to specify a personal_api_key to use feature flags.")
        if not self.feature_flags:
            self.load_feature_flags()

        # If loading in previous line failed
        if not self.feature_flags:
            response = default
        else:
            for flag in self.feature_flags:
                if flag["key"] == key:
                    feature_flag = flag
                    break
            else:
                return default

            if feature_flag.get("is_simple_flag"):
                response = _hash(key, distinct_id) <= ((feature_flag.get("rollout_percentage", 100) or 100) / 100)
            else:
                try:
                    request_data = {
                        "distinct_id": distinct_id,
                        "personal_api_key": self.personal_api_key,
                        "groups": groups,
                    }
                    resp_data = decide(self.api_key, self.host, timeout=10, **request_data)
                    response = key in resp_data["featureFlags"]
                except Exception as e:
                    response = default
                    self.log.warning(
                        "[FEATURE FLAGS] Unable to get data for flag %s, because of the following error:" % key
                    )
                    self.log.warning(e)

        self.capture(distinct_id, "$feature_flag_called", {"$feature_flag": key, "$feature_flag_response": response})
        return response


# This function takes a distinct_id and a feature flag key and returns a float between 0 and 1.
# Given the same distinct_id and key, it'll always return the same float. These floats are
# uniformly distributed between 0 and 1, so if we want to show this feature to 20% of traffic
# we can do _hash(key, distinct_id) < 0.2
def _hash(key, distinct_id):
    hash_key = "%s.%s" % (key, distinct_id)
    hash_val = int(hashlib.sha1(hash_key.encode("utf-8")).hexdigest()[:15], 16)
    return hash_val / __LONG_SCALE__


def require(name, field, data_type):
    """Require that the named `field` has the right `data_type`"""
    if not isinstance(field, data_type):
        msg = "{0} must have {1}, got: {2}".format(name, data_type, field)
        raise AssertionError(msg)


def stringify_id(val):
    if val is None:
        return None
    if isinstance(val, string_types):
        return val
    return str(val)
