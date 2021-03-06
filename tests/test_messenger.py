import sys
import contextlib
import io
import logging
try:
    import queue
except ImportError:
    import Queue as queue
import re
import threading
import time
import uuid

import pytest
import zmq

import networkzero as nw0
_logger = nw0.core.get_logger("networkzero.tests")
nw0.core._enable_debug_logging()

roles = nw0.sockets.Sockets.roles

class SupportThread(threading.Thread):
    """Fake the other end of the message/command/notification chain
    
    NB we use as little as possible of the nw0 machinery here,
    mostly to avoid the possibility of complicated cross-thread
    interference but also to test our own code.
    """
    
    def __init__(self, context):
        threading.Thread.__init__(self)
        _logger.debug("Init thread")
        self.context = context
        self.queue = queue.Queue()
        self.setDaemon(True)
    
    def run(self):
        try:
            while True:
                test_name, args = self.queue.get()
                if test_name is None:
                    break
                _logger.info("test_support: %s, %s", test_name, args)
                function = getattr(self, "support_test_" + test_name)
                function(*args)
        except:
            _logger.exception("Problem in thread")

    def support_test_send_message_to(self, address):
        with self.context.socket(roles['listener']) as socket:
            socket.bind("tcp://%s" % address)
            message = nw0.sockets._unserialise(socket.recv())
            socket.send(nw0.sockets._serialise(message))

    def support_test_wait_for_message_from(self, address, message):
        with self.context.socket(roles['speaker']) as socket:
            socket.connect("tcp://%s" % address)
            _logger.debug("About to send %s", message)
            socket.send(nw0.sockets._serialise(message))
            socket.recv()

    def support_test_wait_for_message_from_with_autoreply(self, address, q):
        with self.context.socket(roles['speaker']) as socket:
            socket.connect("tcp://%s" % address)
            message = uuid.uuid4().hex
            socket.send(nw0.sockets._serialise(message))
            reply = nw0.sockets._unserialise(socket.recv())
            q.put(reply)

    def support_test_send_reply_to(self, address, queue):
        message = uuid.uuid4().hex
        with self.context.socket(roles['speaker']) as socket:
            socket.connect("tcp://%s" % address)
            socket.send(nw0.sockets._serialise(message))
            reply = nw0.sockets._unserialise(socket.recv())
        queue.put(reply)

    def support_test_send_notification_to(self, address, topic, queue):
        with self.context.socket(roles['subscriber']) as socket:
            socket.connect("tcp://%s" % address)
            socket.subscribe = topic.encode("utf-8")
            while True:
                topic, data = nw0.sockets._unserialise_for_pubsub(socket.recv_multipart())
                queue.put((topic, data))
                if data is not None:
                    break

    def support_test_wait_for_notification_from(self, address, topic, data, sync_queue):
        with self.context.socket(roles['publisher']) as socket:
            socket.bind("tcp://%s" % address)
            while True:
                socket.send_multipart(nw0.sockets._serialise_for_pubsub(topic, None))
                try:
                    sync = sync_queue.get_nowait()
                except queue.Empty:
                    time.sleep(0.1)
                else:
                    break
                
            socket.send_multipart(nw0.sockets._serialise_for_pubsub(topic, data))

    def support_test_send_to_multiple_addresses(self, address1, address2):
        poller = zmq.Poller()

        socket1 = self.context.socket(roles['listener'])
        socket2 = self.context.socket(roles['listener'])
        try:
            socket1.bind("tcp://%s" % address1)
            socket2.bind("tcp://%s" % address2)
            poller.register(socket1, zmq.POLLIN)
            poller.register(socket2, zmq.POLLIN)
            polled = dict(poller.poll(2000))
            if socket1 in polled:
                socket1.recv()
                socket1.send(nw0.sockets._serialise(address1))
            elif socket2 in polled:
                socket2.recv()
                socket2.send(nw0.sockets._serialise(address2))
            else:
                raise RuntimeError("Nothing found")
        finally:
            socket1.close()
            socket2.close()

    def support_test_wait_for_notification_from_multiple_addresses(self, address1, address2, topic, data, sync_queue):
        socket1 = self.context.socket(roles['publisher'])
        socket2 = self.context.socket(roles['publisher'])
        try:
            socket1.bind("tcp://%s" % address1)
            socket2.bind("tcp://%s" % address2)
            while True:
                socket1.send_multipart(nw0.sockets._serialise_for_pubsub(topic, None))
                try:
                    sync = sync_queue.get_nowait()
                except queue.Empty:
                    time.sleep(0.1)
                else:
                    break
            socket1.send_multipart(nw0.sockets._serialise_for_pubsub(topic, data))
            socket2.send_multipart(nw0.sockets._serialise_for_pubsub(topic, data))
        finally:
            socket1.close()
            socket2.close()

@pytest.fixture
def support(request):
    thread = SupportThread(nw0.sockets.context)
    def finalise():
        thread.queue.put((None, None))
        thread.join()
    thread.start()
    return thread

@contextlib.contextmanager
def capture_logging(logger, stream):
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.setLevel(logging.WARN)
    logger.addHandler(handler)
    yield
    logger.removeHandler(handler)

def check_log(logger, pattern):
    return bool(re.search(pattern, logger.getvalue()))

#
# send_message_to
#
def test_send_message(support):
    address = nw0.core.address()
    message = uuid.uuid4().hex
    support.queue.put(("send_message_to", [address]))
    reply = nw0.send_message_to(address, message)
    assert reply == message

def test_send_message_with_timeout(support):
    address = nw0.core.address()
    with pytest.raises(nw0.core.SocketTimedOutError):
        nw0.send_message_to(address, wait_for_reply_s=1.0)

def test_send_message_empty(support):
    address = nw0.core.address()
    support.queue.put(("send_message_to", [address]))
    reply = nw0.send_message_to(address)
    assert reply == nw0.messenger.EMPTY

#
# wait_for_message_from
#

def test_wait_for_message(support):
    address = nw0.core.address()
    message_sent = uuid.uuid4().hex
    support.queue.put(("wait_for_message_from", [address, message_sent]))
    message_received = nw0.wait_for_message_from(address, wait_for_s=5)
    assert message_received == message_sent
    nw0.send_reply_to(address, message_received)

def test_wait_for_message_with_timeout():
    address = nw0.core.address()
    message = nw0.wait_for_message_from(address, wait_for_s=0.1)
    assert message is None

def test_wait_for_message_with_autoreply(support):
    address = nw0.core.address()
    reply_queue = queue.Queue()
    support.queue.put(("wait_for_message_from_with_autoreply", [address, reply_queue]))
    nw0.wait_for_message_from(address, autoreply=True)
    assert reply_queue.get() == nw0.messenger.EMPTY
    
#
# send_reply_to
#
def test_send_reply(support):
    address = nw0.core.address()
    reply_queue = queue.Queue()

    support.queue.put(("send_reply_to", [address, reply_queue]))
    message_received = nw0.wait_for_message_from(address, wait_for_s=5)
    nw0.send_reply_to(address, message_received)
    reply = reply_queue.get()
    assert reply == message_received

#
# send_notification_to
#
def test_send_notification(support):
    address = nw0.core.address()
    topic = uuid.uuid4().hex
    data = uuid.uuid4().hex
    reply_queue = queue.Queue()
    
    support.queue.put(("send_notification_to", [address, topic, reply_queue]))
    while True:
        nw0.send_notification_to(address, topic, None)
        try:
            in_topic, in_data = reply_queue.get_nowait()
        except queue.Empty:
            time.sleep(0.1)
        else:
            break

    nw0.send_notification_to(address, topic, data)
    while in_data is None:
        in_topic, in_data = reply_queue.get()
    
    assert in_topic, in_data == (topic, data)

#
# wait_for_notification_from
#
def test_wait_for_notification(support):
    address = nw0.core.address()
    topic = uuid.uuid4().hex
    data = uuid.uuid4().hex
    sync_queue = queue.Queue()
    
    support.queue.put(("wait_for_notification_from", [address, topic, data, sync_queue]))
    in_topic, in_data = nw0.wait_for_notification_from(address, topic, wait_for_s=5)
    sync_queue.put(True)
    while in_data is None:
        in_topic, in_data = nw0.wait_for_notification_from(address, topic, wait_for_s=5)
    assert (topic, data) == (in_topic, in_data)

#
# send to multiple addresses
# For now, this is disallowed as the semantics aren't
# intuitive. (It does a round-robin selection which is useful
# for things like load-scheduling but not for broadcasting).
#
def test_send_to_multiple_addresses(support):
    address1 = nw0.core.address()
    address2 = nw0.core.address()
    message = uuid.uuid4().hex
    with pytest.raises(nw0.core.InvalidAddressError):
        nw0.send_message_to([address1, address2], message)

#
# Wait for notifications from multiple addresses
#
def test_wait_for_notification_from_multiple_addresses(support):
    address1 = nw0.core.address()
    address2 = nw0.core.address()
    topic = uuid.uuid4().hex
    data = uuid.uuid4().hex
    sync_queue = queue.Queue()
    
    support.queue.put(("wait_for_notification_from_multiple_addresses", [address1, address2, topic, data, sync_queue]))
    
    in_topic, in_data = nw0.wait_for_notification_from([address1, address2], topic, wait_for_s=5)        
    sync_queue.put(True)
    while in_data is None:
        in_topic, in_data = nw0.wait_for_notification_from([address1, address2], topic, wait_for_s=5)
    assert (topic, data) == (in_topic, in_data)    
    in_topic, in_data = nw0.wait_for_notification_from([address1, address2], topic, wait_for_s=5)
    assert (topic, data) == (in_topic, in_data)    
