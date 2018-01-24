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
import bisect
import copy

root = logging.getLogger()
while len(root.handlers):
    root.removeHandler(root.handlers[0])

f = logging.Formatter("%(asctime)s - %(module)s.   %(funcName)s - %(levelname)s - %(message)s")
fh = logging.StreamHandler()
fh.setFormatter(f)
root.addHandler(fh)
root.setLevel(logging.DEBUG)


class ScheduleCommand(object):
    def __init__(self, name, t, operation="read", data=None):
        self.operation = operation
        self.name = name
        self.data = data
        self.t = t

    def __eq__(self, other):
        if type(other) == str:
            return self.name == other
        else:
            return self.t == other.t

    def __ne__(self, other):
        return self.t != other.t

    def __gt__(self, other):
        return self.t > other.t

    def __ge__(self, other):
        return self.t >= other.t

    def __lt__(self, other):
        return self.t < other.t

    def __le__(self, other):
        return self.t <= other.t

    def __str__(self):
        return "Schedule command: {0} {1} at time {2}".format(self.operation, self.name, self.t)


class CameraDeviceController(object):
    def __init__(self, device_name):
        self.device_name = device_name
        self.device = None

        self.lock = threading.Lock()

        self.watchdog_timer = None
        self.watchdog_timeout = 3.0
        self.state = pt.DevState.UNKNOWN

        self.schedule_list = list()
        self.schedule_timer = None

        self.attributes = dict()

        self.connect()

    def connect(self):
        self.disconnect()
        root.info("Connecting to {0}".format(self.device_name))
        self.device = None
        self.state = pt.DevState.UNKNOWN
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
            self.state = pt.DevState.UNKNOWN
            self.device = None
            return
        try:
            dev = dev_future.result()
        except pt.DevFailed as e:
            root.error("Device future devfailed {0}".format(str(e)))
            if e[0].reason == "API_DeviceNotExported":
                self.state = pt.DevState.UNKNOWN
                self.device = None
            else:
                raise
        self.device = dev
        self.state = pt.DevState.ON
        self.reset_watchdog()
        self.setup_attributes()
        self.process_schedule()

    def setup_attributes(self):
        root.info("Setting up periodic read attributes dict")
        self.add_polled_attribute("gain", 1.0)
        self.add_polled_attribute("exposuretime", 1.0)
        self.add_polled_attribute("image", 0.5)
        self.add_polled_attribute("state", 3.0)

    def _read_attribute(self, attr_name):
        root.info("Sending read attribute {0} to device".format(attr_name))
        if self.device is not None:
            try:
                attr_future = self.device.read_attribute(attr_name, wait=False)
            except pt.DevFailed as e:
                root.error("read_attribute returned error {0}".format(str(e)))
                return False
            attr_future.add_done_callback(self._read_attribute_cb)

    def _read_attribute_cb(self, attr_future):
        root.info("read_attribute callback")
        if attr_future.cancelled() is True:
            root.error("Attribute future cancelled")
            return
        try:
            attr = attr_future.result()
            root.debug("Attribute {0} result received".format(attr.name))
            attr_name = attr.name.lower()
        except pt.DevFailed as e:
            root.error("Attribute future devfailed {0}".format(str(e)))
            if e[0].reason == "API_DeviceNotExported":
                self.state = pt.DevState.UNKNOWN
                self.device = None
                return
            elif e[0].reason == "API_DeviceTimedOut":
                self.state = pt.DevState.UNKNOWN
                self.device = None
                return
            else:
                pt.Except.re_throw_exception(e, "", "", "")
        if attr_name in self.attributes:
            with self.lock:
                t = self.attributes[attr_name][1]
                self.attributes[attr_name] = (attr, self.attributes[attr_name][1])
            if t is not None:
                sch_cmd = ScheduleCommand(attr_name, t + time.time(), "read")
                with self.lock:
                    bisect.insort(self.schedule_list, sch_cmd)
                    if self.schedule_timer is not None:
                        self.schedule_timer.cancel()
                    self.schedule_timer = threading.Timer(self.schedule_list[0].t - time.time(), self.process_schedule)
                    self.schedule_timer.start()
        else:
            with self.lock:
                self.attributes[attr_name] = (attr, None)
        self.reset_watchdog()

    def _write_attribute(self, attr_name, value):
        if self.device is not None:
            try:
                attr_future = self.device.write_attribute(attr_name, value, wait=False)
            except pt.DevFailed as e:
                root.error("write_attribute returned error {0}".format(str(e)))
                return False
            attr_future.add_done_callback(self._write_attribute_cb)

    def _write_attribute_cb(self, attr_future):
        root.info("write_attribute callback")
        if attr_future.cancelled() is True:
            root.error("Attribute future cancelled")
            return
        try:
            attr = attr_future.result()
            root.debug("Attribute {0} result received".format(attr.name))
            self.read_attribute(attr_future.name)
        except pt.DevFailed as e:
            root.error("Attribute future devfailed {0}".format(str(e)))
            if e[0].reason == "API_DeviceNotExported":
                self.state = pt.DevState.UNKNOWN
                self.device = None
                return
            elif e[0].reason == "API_DeviceTimedOut":
                self.state = pt.DevState.UNKNOWN
                self.device = None
                return
            else:
                pt.Except.re_throw_exception(e, "", "", "")

    def _exec_command(self, cmd_name, value):
        if self.device is not None:
            try:
                cmd_future = self.device.command_inout(cmd_name, value, wait=False)
            except pt.DevFailed as e:
                root.error("exec_command returned error {0}".format(str(e)))
                return False
            cmd_future.add_done_callback(self._exec_command_cb)

    def _exec_command_cb(self, cmd_future):
        root.info("exec_command callback")
        if cmd_future.cancelled() is True:
            root.error("Command future cancelled")
            return
        try:
            result = cmd_future.result()
            root.debug("Command result received: {0}".format(result))
        except pt.DevFailed as e:
            root.error("Command future devfailed {0}".format(str(e)))
            if e[0].reason == "API_DeviceNotExported":
                self.state = pt.DevState.UNKNOWN
                self.device = None
                return
            elif e[0].reason == "API_DeviceTimedOut":
                self.state = pt.DevState.UNKNOWN
                self.device = None
                return
            else:
                pt.Except.re_throw_exception(e, "", "", "")

    def process_schedule(self):
        root.info("Entering process_schedule")
        root.debug("Schedule list length: {0}".format(len(self.schedule_list)))
        t = time.time()
        if self.schedule_timer is not None:
            with self.lock:
                self.schedule_timer.cancel()
                self.schedule_timer = None
        if self.state in [pt.DevState.ON, pt.DevState.RUNNING, pt.DevState.ALARM, pt.DevState.FAULT,
                          pt.DevState.OFF, pt.DevState.STANDBY]:
            try:
                with self.lock:
                    schedule_item = self.schedule_list[0]
            except IndexError:
                root.debug("The schedule was empty. Re-populate.")
                # The schedule was empty. Re-populate.
                # self.setup_attributes()
                return

            while schedule_item.t < time.time():
                root.debug("Processing {0}".format(schedule_item.name))

                try:
                    with self.lock:
                        schedule_item = self.schedule_list.pop(0)
                except IndexError:
                    root.debug("The schedule was empty. Stop looking for attributes")
                    return

                if schedule_item.operation == "read":
                    self._read_attribute(schedule_item.name)
                elif schedule_item.operation == "write":
                    self._write_attribute(schedule_item.name, schedule_item.data)
                elif schedule_item.operation == "command":
                    self._exec_command(schedule_item.name, schedule_item.data)

            # Determine when the next scheduled operation is expected:
            try:
                with self.lock:
                    next_time = self.schedule_list[0].t - t
                root.debug("Next process_schedule in {0} s".format(next_time))
            except IndexError:
                root.debug("The schedule was empty. No new process time")
                return
            with self.lock:
                self.schedule_timer = threading.Timer(next_time, self.process_schedule)
                self.schedule_timer.start()

    def reset_watchdog(self):
        root.info("Resetting watchdog timer")
        if self.watchdog_timer is not None:
            self.watchdog_timer.cancel()
        self.watchdog_timer = threading.Timer(self.watchdog_timeout, self.watchdog_handler)
        self.watchdog_timer.start()

    def stop_watchdog(self):
        if self.watchdog_timer is not None:
            self.watchdog_timer.cancel()

    def watchdog_handler(self):
        root.debug("Watchdog timed out. ")
        if self.state != pt.DevState.INIT:
            self.state = pt.DevState.INIT
            self.exec_command("init")
            self.read_attribute("state")
        else:
            self.connect()

    def get_attribute(self, attr_name):
        if attr_name in self.attributes:
            with self.lock:
                attr = self.attributes[attr_name][0]
            return attr

    def read_attribute(self, attr_name):
        root.info("Read attribute {0}".format(attr_name))
        if attr_name not in self.schedule_list:
            t = time.time()
            sch_cmd = ScheduleCommand(attr_name, t, "read")
            with self.lock:
                bisect.insort(self.schedule_list, sch_cmd)
            self.process_schedule()

    def write_attribute(self, attr_name, value):
        root.info("Write attribute {0} with {1}".format(attr_name, value))
        if attr_name not in self.schedule_list:
            t = time.time()
            sch_cmd = ScheduleCommand(attr_name, t, "write", value)
            with self.lock:
                bisect.insort(self.schedule_list, sch_cmd)
            self.process_schedule()

    def exec_command(self, cmd_name, value=None):
        root.info("Execute command {0} with {1}".format(cmd_name, value))
        if cmd_name not in self.schedule_list:
            t = time.time()
            sch_cmd = ScheduleCommand(cmd_name, t, "command", value)
            with self.lock:
                bisect.insort(self.schedule_list, sch_cmd)
            self.process_schedule()

    def add_polled_attribute(self, attr_name, period):
        root.info("Adding attribute {0} with polling period {1} s".format(attr_name, period))
        with self.lock:
            self.attributes[attr_name] = (None, period)
            sch_cmd = ScheduleCommand(attr_name, time.time(), "read")
            bisect.insort(self.schedule_list, sch_cmd)

    def get_state(self):
        return self.state

    def disconnect(self):
        try:
            self.watchdog_timer.cancel()
        except AttributeError:
            pass
        with self.lock:
            self.schedule_list = []
            try:
                self.schedule_timer.cancel()
            except AttributeError:
                pass
        self.device = None
        self.state = pt.DevState.UNKNOWN


if __name__ == "__main__":
    cam = CameraDeviceController("gunlaser/cameras/jai_test")
