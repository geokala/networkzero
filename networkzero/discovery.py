# -*- coding: utf-8 -*-
import os, sys
import atexit
import collections
import csv
import io
import json
import random
import socket
import threading
import time

import zmq

from . import config
from .core import context
from .logging import logger

def unpack(message):
    return json.loads(message.decode(config.ENCODING))

def pack(message):
    return json.dumps(message).encode(config.ENCODING)
    
class Beacon(threading.Thread):
    
    rpc_port = 9998
    beacon_port = 9999
    finder_timeout_secs = 0.5
    beacon_message_size = 256
    interval_secs = 2
    
    def __init__(self):
        super().__init__(daemon=True)
        
        self._stop_event = threading.Event()
        self._services_to_advertise = {}
        self._services_found = {}
        self._lock = threading.Lock()
        
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.socket.bind(("", self.beacon_port))
        self.socket_fd = self.socket.fileno()
        self.poller = zmq.Poller()
        self.poller.register(self.socket, zmq.POLLIN)
        
        self.rpc = context.socket(zmq.REP)
        self.rpc.bind("tcp://*:%s" % self.rpc_port)

    def stop(self):
        logger.debug("About to stop")
        self._stop_event.set()

    #
    # Commands available via RPC are methods whose
    # name starts with "do_"
    #
    def do_advertise(self, name, port, ip=None):
        logger.debug("Advertise %s on %s", name, port)
        with self._lock:
            self._services_to_advertise.setdefault(name, set()).add(port)
        return name + "!!"
    
    def do_unadvertise(self, name, port, ip=None):
        logger.debug("Unadvertise %s on %s", name, port)
        
        with self._lock:
            ports = self._services_to_advertise.get(name, set())
        
        if not ports:
            logger.warn("Not currently advertising %s on %s", name, port)
            return

        with self._lock:
            ports.remove(port)
            if not ports:
                del self._services_to_advertise[name]
    
    def do_discover(self, name, wait_for_secs):
        logger.debug("Discover %s waiting for %s secs", name, wait_for_secs)
        t1 = time.time() + wait_for_secs
        while True:
            with self._lock:
                discovered = self._services_found.get(name)
                if discovered:
                    break
            if time.time() > t1:
                logger.warn("%s not discovered after %s secs", name, wait_for_secs)
                return None
        
        services = list(discovered)
        return random.choice(services)

    #
    # Main loop:
    # * Check for incoming RPC commands
    # * Check for broadcast adverts
    # * Broadcast any adverts of our own
    #
    def check_for_commands(self, wait=True):
        """The rpc socket will receive a utf-8, json-encoded command
        with 1 or more segments. The first is always an action; any others
        are the parameters.
        
        The actions result in methods being called on this instance; the result
        of a method is re-encoded as json and passed back to the socket.
        """
        try:
            message = self.rpc.recv(0 if wait else zmq.NOBLOCK)
        except zmq.ZMQError as exc:
            if exc.errno == zmq.EAGAIN:
                return
            else:
                raise
        
        logger.debug("Received command %s", message)
        segments = unpack(message)
        action, params = segments[0], segments[1:]
        function = getattr(self, "do_" + action.lower(), None)
        if not function:
            raise NotImplementedError
        else:
            logger.debug("Calling %s with %s", function, params)
            result = function(*params)
            self.rpc.send(pack(result))
    
    def check_for_adverts(self):
        events = dict(self.poller.poll(1000 * self.finder_timeout_secs))
        if self.socket_fd not in events: 
            return

        message, source = self.socket.recvfrom(self.beacon_message_size)
        service_name, service_port = unpack(message)
        service_ip, _  = source
        logger.debug("Advert received from %s for %s on %s", service_ip, service_name, service_port)
        with self._lock:
            self._services_found.setdefault(service_name, set()).add((service_ip, service_port))

    def advertise_names(self):
        with self._lock:
            for service_name, service_ports in self._services_to_advertise.items():
                for service_port in service_ports:
                    logger.debug("Advertising %s on %s", service_name, service_port)
                    message = pack([service_name, service_port])
                    self.socket.sendto(message, 0, ("255.255.255.255", self.beacon_port))

    def run(self):
        logger.info("Starting discovery")
        t0 = time.time()
        while not self._stop_event.wait(0):
            self.check_for_commands(wait=False)
            #
            # Advertise before checking for adverts
            # so that an advert called and checked within
            # the same cycle will be found
            #
            if time.time() > t0 + self.interval_secs:
                self.advertise_names()
                t0 = time.time()
            self.check_for_adverts()
        logger.info("Ending discovery")
                
_beacon = None
_remote_beacon = object()

def start_beacon():
    global _beacon
    if _beacon is None:
        logger.debug("About to start beacon")
        try:
            _beacon = Beacon()
        except:
            logger.exception("Unable to start beacon")
            _beacon = _remote_beacon
        else:
            _beacon.start()

def _rpc(action, *args):
    with context.socket(zmq.REQ) as socket:
        socket.connect("tcp://localhost:%s" % Beacon.rpc_port)
        socket.send(pack([action] + list(args)))
        return unpack(socket.recv())

def split_address(address):
    if ":" in address:
        ip, _, port = address.partition(":")
    else:
        ip, port = None, address
    return ip, port

def advertise(name, address):
    start_beacon()
    ip, port = split_address(str(address))
    result = _rpc("advertise", name, port, ip)
    atexit.register(unadvertise, name, port, ip)
    return result

def unadvertise(name, address):
    start_beacon()
    ip, port = split_address(str(address))
    return _rpc("unadvertise", name, port, ip)
    
def discover(name, wait_for_secs=-1):
    start_beacon()
    return _rpc("discover", name, wait_for_secs)

if __name__ == '__main__':
    pass