from datetime import datetime
import time
from uuid import uuid4
import logging
import numbers
import atexit

from dateutil.tz import tzutc
from six import string_types

from analytics.utils import guess_timezone, clean
from analytics.consumer import Consumer
from analytics.s3_consumer import S3Consumer
from analytics.version import VERSION

try:
    import queue
except:
    import Queue as queue


ID_TYPES = (numbers.Number, string_types)


class Client(object):
    """Create a new Segment client.

    upload_size has different meaning, depending on chosen transport.

    For http transport upload_size means number of items to be batched
    in a single POST request to backend.

    For s3 and s3_delete_first transport upload_size means size in bytes of _uncompressed_
    partition of the data. Sane default value is between 10 and 100 MB
    depending on compressability of underlying data.

    s3_delete_first deletes all the contents of the target folder before 
    """
    log = logging.getLogger('segment')

    def __init__(self, write_key=None, debug=False, max_queue_size=10000,
                 send=True, on_error=None, endpoint=None, upload_size=100,
                 transport='http', key_decorator=lambda x: x):
        require('write_key', write_key, string_types)
        self.queue = queue.Queue(max_queue_size)
        self.write_key = write_key
        self.endpoint = endpoint
        self.on_error = on_error
        self.debug = debug
        self.send = send

        self.transport = transport
        if transport == 'http':
            self.consumer = Consumer(self.queue, write_key, endpoint=endpoint,
                                     on_error=on_error, upload_size=upload_size)
        elif transport == 's3':
            self.consumer = S3Consumer(self.queue, write_key, endpoint=endpoint,
                                       on_error=on_error, upload_size=upload_size,
                                       key_decorator=key_decorator)
        elif transport == 's3_delete_first':
            self.consumer = S3Consumer(self.queue, write_key, endpoint=endpoint,
                                       on_error=on_error, upload_size=upload_size,
                                       key_decorator=key_decorator, delete_first=True)
        else:
            raise ValueError("transport should be either http, s3 or s3_delete_first")

        if debug:
            self.log.setLevel(logging.DEBUG)

        # if we've disabled sending, just don't start the consumer
        if send:
            # On program exit, allow the consumer thread to exit cleanly.
            # This prevents exceptions and a messy shutdown when the interpreter is
            # destroyed before the daemon thread finishes execution. However, it
            # is *not* the same as flushing the queue! To guarantee all messages
            # have been delivered, you'll still need to call flush().
            atexit.register(self.join)
            self.consumer.start()

    def identify(self, user_id=None, traits=None, context=None, timestamp=None,
                 anonymous_id=None, integrations=None, message_id=None):
        traits = traits or {}
        context = context or {}
        integrations = integrations or {}
        require('user_id or anonymous_id', user_id or anonymous_id, ID_TYPES)
        require('traits', traits, dict)

        msg = {
            'integrations': integrations,
            'anonymousId': anonymous_id,
            'timestamp': timestamp,
            'context': context,
            'type': 'identify',
            'userId': user_id,
            'traits': traits,
            'messageId': message_id
        }

        return self._enqueue(msg)

    def track(self, user_id=None, event=None, properties=None, context=None,
              timestamp=None, anonymous_id=None, integrations=None,
              message_id=None):
        properties = properties or {}
        context = context or {}
        integrations = integrations or {}
        require('user_id or anonymous_id', user_id or anonymous_id, ID_TYPES)
        require('properties', properties, dict)
        require('event', event, string_types)

        msg = {
            'integrations': integrations,
            'anonymousId': anonymous_id,
            'properties': properties,
            'timestamp': timestamp,
            'context': context,
            'userId': user_id,
            'type': 'track',
            'event': event,
            'messageId': message_id
        }

        return self._enqueue(msg)

    def alias(self, previous_id=None, user_id=None, context=None,
              timestamp=None, integrations=None, message_id=None):
        context = context or {}
        integrations = integrations or {}
        require('previous_id', previous_id, ID_TYPES)
        require('user_id', user_id, ID_TYPES)

        msg = {
            'integrations': integrations,
            'previousId': previous_id,
            'timestamp': timestamp,
            'context': context,
            'userId': user_id,
            'type': 'alias',
            'messageId': message_id
        }

        return self._enqueue(msg)

    def group(self, user_id=None, group_id=None, traits=None, context=None,
              timestamp=None, anonymous_id=None, integrations=None,
              message_id=None):
        traits = traits or {}
        context = context or {}
        integrations = integrations or {}
        require('user_id or anonymous_id', user_id or anonymous_id, ID_TYPES)
        require('group_id', group_id, ID_TYPES)
        require('traits', traits, dict)

        msg = {
            'integrations': integrations,
            'anonymousId': anonymous_id,
            'timestamp': timestamp,
            'groupId': group_id,
            'context': context,
            'userId': user_id,
            'traits': traits,
            'type': 'group',
            'messageId': message_id
        }

        return self._enqueue(msg)

    def page(self, user_id=None, category=None, name=None, properties=None,
             context=None, timestamp=None, anonymous_id=None,
             integrations=None, message_id=None):
        properties = properties or {}
        context = context or {}
        integrations = integrations or {}
        require('user_id or anonymous_id', user_id or anonymous_id, ID_TYPES)
        require('properties', properties, dict)

        if name:
            require('name', name, string_types)
        if category:
            require('category', category, string_types)

        msg = {
            'integrations': integrations,
            'anonymousId': anonymous_id,
            'properties': properties,
            'timestamp': timestamp,
            'category': category,
            'context': context,
            'userId': user_id,
            'type': 'page',
            'name': name,
            'messageId': message_id
        }

        return self._enqueue(msg)

    def screen(self, user_id=None, category=None, name=None, properties=None,
               context=None, timestamp=None, anonymous_id=None,
               integrations=None):
        properties = properties or {}
        context = context or {}
        integrations = integrations or {}
        require('user_id or anonymous_id', user_id or anonymous_id, ID_TYPES)
        require('properties', properties, dict)

        if name:
            require('name', name, string_types)
        if category:
            require('category', category, string_types)

        msg = {
            'integrations': integrations,
            'anonymousId': anonymous_id,
            'properties': properties,
            'timestamp': timestamp,
            'category': category,
            'context': context,
            'userId': user_id,
            'type': 'screen',
            'name': name,
        }

        return self._enqueue(msg)

    def _enqueue(self, msg):
        """Push a new `msg` onto the queue, return `(success, msg)`"""
        timestamp = msg['timestamp']
        if timestamp is None:
            # milliseconds since the Epoch
            timestamp = int(time.time()*1000)

        require('integrations', msg['integrations'], dict)
        require('type', msg['type'], string_types)
        require('timestamp', timestamp, int)
        require('context', msg['context'], dict)

        # add common
        msg['timestamp'] = timestamp
        msg['messageId'] = msg.get('messageId') or str(uuid4())
        msg['context']['library'] = {
            'name': 'analytics-python',
            'version': VERSION,
            'transport': self.transport,
        }

        msg = clean(msg)
        self.log.debug('queueing: %s', msg)

        # if send is False, return msg as if it was successfully queued
        if not self.send:
            return True, msg

        try:
            self.queue.put(msg, block=False)
            self.log.debug('enqueued %s.', msg['type'])
            return True, msg
        except queue.Full:
            self.log.warn('analytics-python queue is full')
            return False, msg

    def flush(self):
        """Forces a flush from the internal queue to the server"""
        queue = self.queue
        size = queue.qsize()
        queue.join()
        # Note that this message may not be precise, because of threading.
        self.log.debug('successfully flushed about %s items.', size)

    def join(self):
        """Ends the consumer thread once the queue is empty. Blocks execution until finished"""
        self.consumer.pause()
        try:
            self.consumer.join()
        except RuntimeError:
            # consumer thread has not started
            pass


def require(name, field, data_type):
    """Require that the named `field` has the right `data_type`"""
    if not isinstance(field, data_type):
        msg = '{0} must have {1}, got: {2}'.format(name, data_type, field)
        raise AssertionError(msg)
