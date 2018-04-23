# -*- coding:utf-8 -*-
"""
Created on Apr 13, 2018

@author: Filip Lindau
"""
import threading
import time
import logging
import traceback
import Queue
from concurrent.futures import Future
from twisted.internet import reactor, defer, error
from twisted.internet.protocol import Protocol, ClientFactory, Factory
from twisted.python.failure import Failure, reflect
import PyTango as tango
import PyTango.futures as tangof
import TangoTwisted
from TangoTwisted import TangoAttributeFactory, TangoAttributeProtocol, \
    LoopingCall, DeferredCondition, ClockReactorless, defer_later
import numpy as np

logger = logging.getLogger("SpectrometerCameraController")
while len(logger.handlers):
    logger.removeHandler(logger.handlers[0])

# f = logging.Formatter("%(asctime)s - %(module)s.   %(funcName)s - %(levelname)s - %(message)s")
f = logging.Formatter("%(asctime)s - %(name)s.   %(funcName)s - %(levelname)s - %(message)s")
fh = logging.StreamHandler()
fh.setFormatter(f)
logger.addHandler(fh)
logger.setLevel(logging.DEBUG)


class MonitorAttribute(object):
    def __init__(self, attr_name, dev_name, period):
        self.attr_name = attr_name
        self.dev_name = dev_name
        self.period = period
        self.running = True
        self.result = None

        self.logger = logging.getLogger("SpectrometerCameraController.MonitorAttribute")
        self.logger.setLevel(logging.WARNING)
        self.logger.info("MonitorAttribute.__init__")

    def stop(self):
        self.logger.info("Stop monitor of {0}".format(self.attr_name))
        self.running = False

    def start(self):
        self.logger.info("Start monitor of {0}".format(self.attr_name))
        self.running = True

    def data_received(self, result):
        self.logger.debug("Monitor attribute {0} result received".format(self.attr_name))
        self.result = result

    def get_next_delay(self):
        t = time.time()
        if self.result is None:
            return 0
        t_elapsed = t - self.result.time.totime()
        dt = self.period - t_elapsed
        if dt < 0:
            return 0
        return dt


class SpectrometerCameraController(object):
    def __init__(self, camera_name, start=False):
        """
        Controller for running a spectrometer. Communicates with a camera looking at a spectrum.


        :param camera_name: Tango name for the camera device
        :param start: True if the device factory is started during controller object creation
        """
        self.device_names = dict()
        self.device_names["camera"] = camera_name

        self.device_factory_dict = dict()

        self.logger = logging.getLogger("SpectrometerCameraController.Controller")
        self.logger.setLevel(logging.WARNING)
        self.logger.info("SpectrometerCameraController.__init__")

        self.running_attr_params = dict()
        self.running_attr_params["state"] = 0.2
        self.running_attr_params["gain"] = 2.0
        self.running_attr_params["exposuretime"] = 2.0
        self.running_attr_params["image"] = 0.1

        self.standby_attr_params = dict()
        self.standby_attr_params["state"] = 0.2
        self.standby_attr_params["gain"] = 2.0
        self.standby_attr_params["exposuretime"] = 2.0

        # Dictionary where read and constructed tango attributes are stored.
        # Assume None or tango.DeviceAttribute
        self.camera_result = dict()
        self.camera_result["state"] = None
        self.camera_result["gain"] = None
        self.camera_result["exposuretime"] = None
        self.camera_result["image"] = None
        self.camera_result["wavelegnths"] = None
        self.camera_result["width"] = None
        self.camera_result["peak"] = None
        self.camera_result["satlvl"] = None
        self.camera_result["max_value"] = None
        self.camera_result["spectrum"] = None

        self.looping_calls = list()

        self.setup_params = dict()
        self.setup_params["triggermode"] = "Off"
        self.setup_params["pixelformat"] = "Mono16"
        self.setup_params["imageoffsetx"] = 0
        self.setup_params["imageoffsety"] = 0
        self.setup_params["imagewidth"] = 1280
        self.setup_params["imageheight"] = 1024
        self.setup_params["framerate"] = 10

        self.state_lock = threading.Lock()
        self.status = ""
        self.state = "unknown"
        self.state_notifier_list = list()       # Methods in this list will be called when the state
        # or status message is changed

        if start is True:
            self.device_factory_dict["camera"] = TangoAttributeFactory(camera_name)

            for dev_fact in self.device_factory_dict:
                self.device_factory_dict[dev_fact].startFactory()

    def read_attribute(self, name, device_name):
        self.logger.info("Read attribute \"{0}\" on \"{1}\"".format(name, device_name))
        if device_name in self.device_names:
            factory = self.device_factory_dict[self.device_names[device_name]]
            d = factory.buildProtocol("read", name)
        else:
            self.logger.error("Device name {0} not found among {1}".format(device_name, self.device_factory_dict))
            err = tango.DevError(reason="Device {0} not used".format(device_name),
                                 severety=tango.ErrSeverity.ERR,
                                 desc="The device is not in the list of devices used by this controller",
                                 origin="read_attribute")
            d = Failure(tango.DevFailed(err))
        return d

    def write_attribute(self, name, device_name, data):
        self.logger.info("Write attribute \"{0}\" on \"{1}\"".format(name, device_name))
        if device_name in self.device_names:
            factory = self.device_factory_dict[self.device_names[device_name]]
            d = factory.buildProtocol("write", name, data)
        else:
            self.logger.error("Device name {0} not found among {1}".format(device_name, self.device_factory_dict))
            err = tango.DevError(reason="Device {0} not used".format(device_name),
                                 severety=tango.ErrSeverity.ERR,
                                 desc="The device is not in the list of devices used by this controller",
                                 origin="write_attribute")
            d = Failure(tango.DevFailed(err))
        return d

    def send_command(self, name, device_name, data):
        self.logger.info("Send command \"{0}\" on \"{1}\"".format(name, device_name))
        if device_name in self.device_names:
            factory = self.device_factory_dict[self.device_names[device_name]]
            d = factory.buildProtocol("command", name, data)
        else:
            self.logger.error("Device name {0} not found among {1}".format(device_name, self.device_factory_dict))
            err = tango.DevError(reason="Device {0} not used".format(device_name),
                                 severety=tango.ErrSeverity.ERR,
                                 desc="The device is not in the list of devices used by this controller",
                                 origin="write_attribute")
            d = Failure(tango.DevFailed(err))
        return d

    def check_attribute(self, attr_name, dev_name, target_value, period=0.3, timeout=1.0, tolerance=None, write=True):
        """
        Check an attribute to see if it reaches a target value. Returns a deferred for the result of the
        check.
        Upon calling the function the target is written to the attribute if the "write" parameter is True.
        Then reading the attribute is polled with the period "period" for a maximum number of retries.
        If the read value is within tolerance, the callback deferred is fired.
        If the read value is outside tolerance after retires attempts, the errback is fired.
        The maximum time to check is then period x retries

        :param attr_name: Tango name of the attribute to check, e.g. "position"
        :param dev_name: Tango device name to use, e.g. "gunlaser/motors/zaber01"
        :param target_value: Attribute value to wait for
        :param period: Polling period when checking the value
        :param timeout: Time to wait for the attribute to reach target value
        :param tolerance: Absolute tolerance for the value to be accepted
        :param write: Set to True if the target value should be written initially
        :return: Deferred that will fire depending on the result of the check
        """
        self.logger.info("Check attribute \"{0}\" on \"{1}\"".format(attr_name, dev_name))
        if dev_name in self.device_names:
            factory = self.device_factory_dict[self.device_names[dev_name]]
            d = factory.buildProtocol("check", attr_name, None, write=write, target_value=target_value,
                                      tolerance=tolerance, period=period, timeout=timeout)
            d.addCallback(self.update_attribute)
        else:
            self.logger.error("Device name {0} not found among {1}".format(dev_name, self.device_factory_dict))
            err = tango.DevError(reason="Device {0} not used".format(dev_name),
                                 severety=tango.ErrSeverity.ERR,
                                 desc="The device is not in the list of devices used by this controller",
                                 origin="check_attribute")
            d = Failure(tango.DevFailed(err))
        return d

    def get_state(self):
        with self.state_lock:
            st = self.state
        return st

    def set_state(self, state):
        with self.state_lock:
            self.state = state
            for m in self.state_notifier_list:
                m(self.state, self.status)

    def get_status(self):
        with self.state_lock:
            st = self.status
        return st

    def set_status(self, status_msg):
        self.logger.debug("Status: {0}".format(status_msg))
        with self.state_lock:
            self.status = status_msg
            for m in self.state_notifier_list:
                m(self.state, self.status)

    def get_attribute(self, attr_name):
        with self.state_lock:
            try:
                res = self.camera_result[attr_name]
            except KeyError:
                res = None
            return res

    def add_state_notifier(self, state_notifier_method):
        self.state_notifier_list.append(state_notifier_method)

    def remove_state_notifier(self, state_notifier_method):
        try:
            self.state_notifier_list.remove(state_notifier_method)
        except ValueError:
            self.logger.warning("Method {0} not in list. Ignoring.".format(state_notifier_method))

    def update_attribute(self, result):
        self.logger.info("Updating attribute with {0}".format(result))
        try:
            attr_name = result.name
        except AttributeError:
            return result
        self.camera_result[attr_name] = result
        return result
