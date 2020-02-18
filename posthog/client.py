from datetime import datetime
from uuid import uuid4
import logging
import numbers
import atexit

from dateutil.tz import tzutc
from six import string_types

from posthog.utils import guess_timezone, clean
from posthog.consumer import Consumer
from posthog.request import post
from posthog.version import VERSION

try:
    import queue
except ImportError:
    import Queue as queue


ID_TYPES = (numbers.Number, string_types)


class Client(object):
    """Create a new PostHog client."""
    log = logging.getLogger('posthog')

    def __init__(self, api_key=None, host=None, debug=False,
                 max_queue_size=10000, send=True, on_error=None, flush_at=100,
                 flush_interval=0.5, gzip=False, max_retries=3,
                 sync_mode=False, timeout=15, thread=1):
        require('api_key', api_key, string_types)

        self.queue = queue.Queue(max_queue_size)
        self.api_key = api_key
        self.on_error = on_error
        self.debug = debug
        self.send = send
        self.sync_mode = sync_mode
        self.host = host
        self.gzip = gzip
        self.timeout = timeout

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
                    self.queue, api_key, host=host, on_error=on_error,
                    flush_at=flush_at, flush_interval=flush_interval,
                    gzip=gzip, retries=max_retries, timeout=timeout,
                )
                self.consumers.append(consumer)

                # if we've disabled sending, just don't start the consumer
                if send:
                    consumer.start()

    def identify(self, distinct_id=None, properties=None, context=None, timestamp=None,
                message_id=None):
        properties = properties or {}
        context = context or {}
        require('distinct_id', distinct_id, ID_TYPES)
        require('properties', properties, dict)

        msg = {
            'timestamp': timestamp,
            'context': context,
            'type': 'identify',
            'distinct_id': distinct_id,
            '$set': properties,
            'event': '$identify',
            'messageId': message_id,
        }

        return self._enqueue(msg)

    def capture(self, distinct_id=None, event=None, properties=None, context=None,
              timestamp=None, message_id=None):
        properties = properties or {}
        context = context or {}
        require('distinct_id', distinct_id, ID_TYPES)
        require('properties', properties, dict)
        require('event', event, string_types)

        msg = {
            'properties': properties,
            'timestamp': timestamp,
            'context': context,
            'distinct_id': distinct_id,
            'type': 'capture',
            'event': event,
            'messageId': message_id,
        }

        return self._enqueue(msg)

    def alias(self, previous_id=None, distinct_id=None, context=None,
              timestamp=None, message_id=None):
        context = context or {}

        require('previous_id', previous_id, ID_TYPES)
        require('distinct_id', distinct_id, ID_TYPES)

        msg = {
            'properties': {
                'distinct_id': previous_id,
                'alias': distinct_id,
            },
            'timestamp': timestamp,
            'context': context,
            'type': 'alias',
            'event': '$create_alias'
        }

        return self._enqueue(msg)

    def group(self, distinct_id=None, group_id=None, traits=None, context=None,
              timestamp=None, message_id=None):
        traits = traits or {}
        context = context or {}

        require('distinct_id', distinct_id, ID_TYPES)
        require('group_id', group_id, ID_TYPES)
        require('traits', traits, dict)

        msg = {
            'timestamp': timestamp,
            'groupId': group_id,
            'context': context,
            'distinct_id': distinct_id,
            'traits': traits,
            'type': 'group',
            'messageId': message_id,
        }

        return self._enqueue(msg)

    def page(self, distinct_id=None, category=None, name=None, properties=None,
            context=None, timestamp=None, message_id=None):
        properties = properties or {}
        context = context or {}

        require('distinct_id', distinct_id, ID_TYPES)
        require('properties', properties, dict)

        if name:
            require('name', name, string_types)
        if category:
            require('category', category, string_types)

        msg = {
            'properties': properties,
            'timestamp': timestamp,
            'category': category,
            'context': context,
            'distinct_id': distinct_id,
            'type': 'page',
            'name': name,
            'messageId': message_id,
        }

        return self._enqueue(msg)

    def screen(self, distinct_id=None, category=None, name=None, properties=None,
               context=None, timestamp=None, message_id=None):
        properties = properties or {}
        context = context or {}

        require('distinct_id', distinct_id, ID_TYPES)
        require('properties', properties, dict)

        if name:
            require('name', name, string_types)
        if category:
            require('category', category, string_types)

        msg = {

            'properties': properties,
            'timestamp': timestamp,
            'category': category,
            'context': context,
            'distinct_id': distinct_id,
            'type': 'screen',
            'name': name,
            'messageId': message_id,
        }

        return self._enqueue(msg)

    def _enqueue(self, msg):
        """Push a new `msg` onto the queue, return `(success, msg)`"""
        timestamp = msg['timestamp']
        if timestamp is None:
            timestamp = datetime.utcnow().replace(tzinfo=tzutc())
        message_id = msg.get('messageId')
        if message_id is None:
            message_id = uuid4()

        require('type', msg['type'], string_types)
        require('timestamp', timestamp, datetime)
        require('context', msg['context'], dict)

        # add common
        timestamp = guess_timezone(timestamp)
        msg['timestamp'] = timestamp.isoformat()
        msg['messageId'] = stringify_id(message_id)
        if not msg.get('properties'):
            msg['properties'] = {}
        msg['properties']['$lib'] = 'posthog-python'
        msg['properties']['$lib_version'] = VERSION

        msg['distinct_id'] = stringify_id(msg.get('distinct_id', None))

        msg = clean(msg)
        self.log.debug('queueing: %s', msg)

        # if send is False, return msg as if it was successfully queued
        if not self.send:
            return True, msg

        if self.sync_mode:
            self.log.debug('enqueued with blocking %s.', msg['type'])
            post(self.api_key, self.host, gzip=self.gzip,
                 timeout=self.timeout, batch=[msg])

            return True, msg

        try:
            self.queue.put(msg, block=False)
            self.log.debug('enqueued %s.', msg['type'])
            return True, msg
        except queue.Full:
            self.log.warning('analytics-python queue is full')
            return False, msg

    def flush(self):
        """Forces a flush from the internal queue to the server"""
        queue = self.queue
        size = queue.qsize()
        queue.join()
        # Note that this message may not be precise, because of threading.
        self.log.debug('successfully flushed about %s items.', size)

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

    def shutdown(self):
        """Flush all messages and cleanly shutdown the client"""
        self.flush()
        self.join()


def require(name, field, data_type):
    """Require that the named `field` has the right `data_type`"""
    if not isinstance(field, data_type):
        msg = '{0} must have {1}, got: {2}'.format(name, data_type, field)
        raise AssertionError(msg)


def stringify_id(val):
    if val is None:
        return None
    if isinstance(val, string_types):
        return val
    return str(val)
