# -*- coding:utf-8 -*-
"""
Created on Jan 18, 2018

@author: Filip Lindau
"""
import threading
import time
import PyTango as pt
import numpy as np
import logging

root = logging.getLogger()
while len(root.handlers):
    root.removeHandler(root.handlers[0])

f = logging.Formatter("%(asctime)s - %(module)s.   %(funcName)s - %(levelname)s - %(message)s")
fh = logging.StreamHandler()
fh.setFormatter(f)
root.addHandler(fh)
root.setLevel(logging.DEBUG)


class Attribute(object):
    def __init__(self, name, device):
        self.name = name
        self.device = device
        self.value = None
        self.subscribers = []

        self.reply_id = None
        self.read_thread = None
        self.stop_thread_flag = False
        self.lock = threading.Lock()

    def get_value(self):
        with self.lock:
            retval = self.value
        return retval

    def read(self):
        if self.reply_id is None:
            try:
                self.reply_id = self.device.read_attribute_asynch(self.name)
            except pt.DevFailed as e:
                root.error("Attribute {0} read_attribute_asynch error {1}".format(self.name, str(e)))
                if e[0].reason == "API_DeviceNotExported":
                    with self.lock:
                        self.value = None
                    self.send_reply()
                return
            self.read_thread = threading.Thread(name=self.name, target=self.read_reply)
            self.read_thread.start()

    def read_reply(self):
        while self.stop_thread_flag is False:
            try:
                reply = self.device.read_attribute_reply(self.reply_id, timeout=100)
            except pt.DevFailed as e:
                root.error("Attribute {0} read_attribute_reply error {1}".format(self.name, str(e)))
                if e[0].reason == "API_AsynReplyNotArrived":
                    time.sleep(0.01)
                    pass
                else:
                    reply = None
            with self.lock:
                self.value = reply
            self.send_reply()
        self.reply_id = None

    def write(self, data):
        try:
            self.device.write_attribute(self.name, data, wait=False)
        except:
            pass

    def add_subscriber(self, subscriber):
        root.info("Adding subscriber {0}".format(str(subscriber)))
        self.subscribers.append(subscriber)

    def remove_subscriber(self, subscriber):
        root.info("Removing subscriber {0}".format(str(subscriber)))
        if subscriber in self.subscribers:
            self.subscribers.remove(subscriber)

    def send_reply(self):
        for subscriber in self.subscribers:
            with self.lock:
                subscriber(self.value)

    def stop_read_thread(self):
        root.info("Stopping read thread")
        self.stop_thread_flag = True


class CameraDeviceController(object):
    def __init__(self, device_name):
        self.device_name = device_name
        self.device = None

        self.watchdog_timer = None
        self.watchdog_timeout = 10.0
        self.state = pt.DevState.UNKNOWN

        self.attributes = dict()

    def connect(self):
        root.info("Connecting to {0}".format(self.device_name))
        try:
            self.device = pt.DeviceProxy(self.device_name)
        except pt.DevFailed:
            self.device = None
            self.state = pt.DevState.UNKNOWN
            return False
        self.state = pt.DevState.ON
        self.reset_watchdog()

    def setup_attributes(self):
        s = "gain"
        if s not in self.attributes:
            attr = Attribute(s, self.device)
            attr.read()
            self.attributes[s] = attr
        s = "exposuretime"
        if s not in self.attributes:
            attr = Attribute(s, self.device)
            attr.read()
            self.attributes[s] = attr
        s = "image"
        if s not in self.attributes:
            attr = Attribute(s, self.device)
            attr.read()
            self.attributes[s] = attr

    def reset_watchdog(self):
        if self.watchdog_timer is not None:
            self.watchdog_timer.cancel()
        self.watchdog_timer = threading.Timer(self.watchdog_timeout, self.watchdog_handler)
        self.watchdog_timer.start()

    def stop_watchdog(self):
        if self.watchdog_timer is not None:
            self.watchdog_timer.cancel()

    def watchdog_handler(self):
        root.debug("Watchdog timed out. ")
        self.connect()

    def get_attribute(self, attr_name):
        if attr_name in self.attributes:
            return self.attributes[attr_name].get_value()

