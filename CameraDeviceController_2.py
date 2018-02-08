# -*- coding:utf-8 -*-
"""
Created on Jan 18, 2018

@author: Filip Lindau
"""
import threading
import time
import PyTango as pt
import PyTango.futures as ptf
import numpy as np
import logging
import DeviceController as dc
import Queue

root = logging.getLogger("CameraDeviceController")
while len(root.handlers):
    root.removeHandler(root.handlers[0])

f = logging.Formatter("%(asctime)s - %(module)s.   %(funcName)s - %(levelname)s - %(message)s")
fh = logging.StreamHandler()
fh.setFormatter(f)
root.addHandler(fh)
root.setLevel(logging.DEBUG)


class CameraDeviceController(object):
    def __init__(self, device_name, parameter_dict=None):
        self.device_name = device_name
        self.device = None

        self.lock = threading.Lock()
        self.state_condition_var = threading.Condition()
        self.wakeup_condition_var = threading.Condition()
        self.watchdog_timer = None
        self.watchdog_timeout = 10.0
        self.state = pt.DevState.UNKNOWN
        self.state_cb_list = list()

        self.attribute_dict = dict()
        self.command_list = list()
        self.parameter_dict = dict()
        self.setup_parameters(parameter_dict)

        self.state_thread = threading.Thread()
        threading.Thread.__init__(self.state_thread, target=self.statehandler_dispatcher)

        self.command_queue = Queue.Queue(100)

        self.statehandler_dict = {pt.DevState.ON: self.on_handler,
                                  pt.DevState.STANDBY: self.standby_handler,
                                  pt.DevState.ALARM: self.on_handler,
                                  pt.DevState.FAULT: self.fault_handler,
                                  pt.DevState.INIT: self.init_handler,
                                  pt.DevState.UNKNOWN: self.unknown_handler,
                                  pt.DevState.OFF: self.off_handler}

        self.stop_state_thread_flag = False
        self.state_thread.start()

    def statehandler_dispatcher(self):
        root.debug("===========================================================")
        root.debug("           Starting statehandler_dispatcher")
        root.debug("===========================================================")
        prev_state = self.get_state()
        while self.stop_state_thread_flag is False:
            try:
                state = self.get_state()
                self.statehandler_dict[state](prev_state)
                prev_state = state
            except KeyError:
                self.statehandler_dict[pt.DevState.UNKNOWN](prev_state)
                prev_state = state
        self.stop_watchdog()
        root.debug("===========================================================")
        root.debug("           Exiting statehandler_dispatcher")
        root.debug("===========================================================")

    def unknown_handler(self, prev_state):
        root.debug("Entering unknown_handler")
        connection_timeout = 1.0

        while self.get_state() == pt.DevState.UNKNOWN and self.stop_state_thread_flag is False:
            with self.state_condition_var:
                self.connect()
                self.state_condition_var.wait(3.0)
            if self.device is not None:
                self.set_state(pt.DevState.INIT)
            else:
                with self.wakeup_condition_var:
                    self.wakeup_condition_var.wait(connection_timeout)

    def init_handler(self, prev_state):
        root.debug("Entering init_handler")
        timeout = 1.0

        root.debug("Setting up initial parameters")
        stop_cmd = dc.DeviceCommand("stop", "command", self.device)
        stop_cmd.start()
        prev_cmd = stop_cmd
        for param in self.parameter_dict:
            cmd = dc.DeviceCommand(param, "write", self.device, self.parameter_dict[param])
            cmd.add_condition(prev_cmd)
            cmd.add_subscriber(self.device_command_cb)
            self.command_list.append(cmd)
            prev_cmd = cmd
            cmd.start()
            cmd = dc.DeviceCommand(param, "read", self.device)
            cmd.add_condition(prev_cmd)
            cmd.add_subscriber(self.device_command_cb)
            self.command_list.append(cmd)
            prev_cmd = cmd
            cmd.start()
        start_cmd = dc.DeviceCommand("start", "command", self.device)
        start_cmd.add_condition(prev_cmd)
        start_cmd.add_subscriber(self.init_parameters_ready)
        start_cmd.start()

        self.reset_watchdog()

        with self.wakeup_condition_var:
            self.wakeup_condition_var.wait(timeout)

        root.info("Setting up periodic read attributes dict")
        self.add_polled_attribute("state", 1.0)
        # self.add_polled_attribute("gain", 1.0)
        # self.add_polled_attribute("exposuretime", 1.0)
        # self.add_polled_attribute("image", 0.5)
        self.set_state(pt.DevState.ON)

    def on_handler(self, prev_state):
        root.debug("Entering on_handler")
        timeout = 1.0
        handled_states = [pt.DevState.ON]
        while self.get_state() in handled_states and self.stop_state_thread_flag is False:
            with self.state_condition_var:
                self.state_condition_var.wait(timeout)

    def off_handler(self, prev_state):
        pass

    def running_handler(self, prev_state):
        pass

    def standby_handler(self, prev_state):
        pass

    def fault_handler(self, prev_state):
        pass

    def connect(self):
        self.disconnect()
        root.info("Connecting to {0}".format(self.device_name))
        self.device = None
        self.set_state(pt.DevState.UNKNOWN)
        try:
            dev_future = ptf.DeviceProxy(self.device_name, wait=False)
        except pt.DevFailed as e:
            root.error("DeviceProxy returned error {0}".format(str(e)))
            return False
        dev_future.add_done_callback(self.connected_cb)

    def connected_cb(self, dev_future):
        root.info("Connected callback")
        if dev_future.cancelled() is True:
            root.error("Device future cancelled")
            self.set_state(pt.DevState.UNKNOWN)
            self.device = None
        else:
            try:
                dev = dev_future.result()
            except pt.DevFailed as e:
                root.error("Device future devfailed {0}".format(str(e)))
                if e[0].reason == "API_DeviceNotExported":
                    self.set_state(pt.DevState.UNKNOWN)
                    dev = None
                else:
                    raise
        self.device = dev
        with self.state_condition_var:
            self.state_condition_var.notify_all()

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
        self.set_state(pt.DevState.UNKNOWN)

    def get_attribute(self, attr_name):
        if attr_name in self.attribute_dict:
            return self.attribute_dict[attr_name].get_attribute()

    def add_polled_attribute(self, attr_name, period):
        root.info("Adding attribute {0} with polling period {1} s".format(attr_name, period))
        with self.lock:
            attr = pt.DeviceAttribute()
            self.attribute_dict[attr_name] = attr
            dev_cmd = dc.DeviceCommand(attr_name, "read", self.device, recurrent=True, period=period)
            self.command_list.append(dev_cmd)
            dev_cmd.add_subscriber(self.device_command_cb)
            dev_cmd.start()

    def get_state(self):
        return self.state

    def set_state(self, new_state):
        root.debug("Setting new state {0}".format(new_state))
        self.state = new_state
        for cb in self.state_cb_list:
            root.debug("Calling state callback {0}".format(cb))
            cb(new_state)
        with self.state_condition_var:
            self.state_condition_var.notify_all()

    def add_state_callback(self, cb):
        root.debug("Adding state callback {0}".format(cb))
        self.state_cb_list.append(cb)

    def disconnect(self):
        try:
            self.watchdog_timer.cancel()
        except AttributeError:
            pass
        with self.lock:
            for cmd in self.command_list:
                cmd.cancel()
            # self.command_list = []
        self.device = None
        self.set_state(pt.DevState.UNKNOWN)

    def stop_thread(self):
        self.stop_state_thread_flag = True
        with self.wakeup_condition_var:
            self.wakeup_condition_var.notify_all()
        self.disconnect()

    def setup_parameters(self, parameter_dict):
        root.debug("Setting up initial parameters for device \"{0}\"".format(self.device_name))
        try:
            for parameter in parameter_dict:
                self.parameter_dict[parameter] = parameter_dict[parameter]
                root.debug("Parameter {0}: {1}".format(parameter, parameter_dict[parameter]))
            if self.get_state() is not pt.DevState.UNKNOWN:
                self.set_state(pt.DevState.INIT)
        except TypeError:
            pass

    def init_parameters_ready(self, cmd):
        with self.wakeup_condition_var:
            self.wakeup_condition_var.notify_all()

    def device_command_cb(self, cmd):
        root.debug("\"{0}\" device_command_cb: \"{1}\" {2}".format(self.device_name, cmd.name, cmd.operation.upper()))
        root.debug("command list length: {0}".format(len(self.command_list)))
        root.debug("received cmd {0}".format(cmd))
        # root.debug("command list: {1}".format(self.command_list))
        if cmd.done is True:
            if cmd in self.command_list:
                root.debug("Command removed")
                self.command_list.remove(cmd)
            if cmd.operation == "read":
                self.attribute_dict[cmd.name] = cmd.attr_result
            self.reset_watchdog()
            if cmd.recurrent is True:
                t = cmd.start_time + cmd.period - time.time()
                delay_cmd = dc.DeviceCommand("delay_{0}".format(cmd.name), "delay", self.device, t)
                cmd.add_condition(delay_cmd)
                delay_cmd.start()
                cmd.start()
                # self.command_list.append(delay_cmd)
                self.command_list.append(cmd)

if __name__ == "__main__":
    params = dict()
    params["imageoffsetx"] = 0
    params["imageoffsety"] = 0
    params["imageheight"] = 300
    params["imagewidth"] = 1000
    params["triggermode"] = "Off"
    cdc = CameraDeviceController("gunlaser/cameras/blackfly_test01", params)
