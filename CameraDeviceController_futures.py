# -*- coding:utf-8 -*-
"""
Created on Jan 18, 2018

@author: Filip Lindau
"""
import threading
import Queue
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
    def __init__(self, name, t, operation="read", data=None, cmd_not_pending_list=[]):
        self.operation = operation
        self.name = name
        self.data = data
        self.t = t
        self.defer_execution = False
        if type(cmd_not_pending_list) is not list:
            cmd_not_pending_list = [cmd_not_pending_list]
        self.command_not_pending_list = cmd_not_pending_list

    def __eq__(self, other):
        if type(other) == str:
            return self.name == other
        else:
            return repr(self) == repr(other)

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
        self.process_after_result_flag = False
        self.process_queue = Queue.Queue(2)

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
        self.add_polled_attribute("state", 0.5)

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
        with self.lock:
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
                t = self.attributes[attr_name][1]
                self.attributes[attr_name] = (attr, self.attributes[attr_name][1])
                if t is not None:
                    sch_cmd = ScheduleCommand(attr_name, t + time.time(), "read")
                    bisect.insort(self.schedule_list, sch_cmd)
                    if self.schedule_timer is not None:
                        self.schedule_timer.cancel()
                    self.schedule_timer = threading.Timer(self.schedule_list[0].t - time.time(), self.process_schedule)
                    self.schedule_timer.start()
            else:
                self.attributes[attr_name] = (attr, None)
            if attr_name == "state":
                self.state = attr.value
        self.reset_watchdog()
        if self.process_after_result_flag is True:
            self.process_schedule()

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
        except AttributeError:
            pass
        if self.process_after_result_flag is True:
            self.process_schedule()

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
        if self.process_after_result_flag is True:
            self.process_schedule()

    def _delay_cb(self, sch_item):
        root.info("delay_command callback")
        with self.lock:
            self.schedule_list.remove(sch_item)
        self.process_schedule()

    def process_schedule(self):
        try:
            self.process_queue.put(1, False)
        except Queue.Full:
            return
        self._process_schedule()

    def _process_schedule(self):
        with self.lock:
            root.info("Entering process_schedule")
            root.debug("Schedule list length: {0}".format(len(self.schedule_list)))
            self.process_after_result_flag = False
            t = time.time()
            next_time = np.finfo(float).max
            if self.schedule_timer is not None:
                    self.schedule_timer.cancel()
                    self.schedule_timer = None
            if self.state not in [pt.DevState.UNKNOWN]:
                execute_list = list()
                for index, schedule_item in enumerate(self.schedule_list):
                    if schedule_item.t < t:
                        root.debug("Processing {0}".format(schedule_item.name))
                        execute = True
                        for cmd_not_pending in schedule_item.command_not_pending_list:
                            if cmd_not_pending in self.schedule_list:
                                root.debug("Do not execute since cmd {0} still in list".format(cmd_not_pending.name))
                                execute = False
                                self.process_after_result_flag = True

                                break
                        if execute is True:
                            # self.schedule_list.pop(index)
                            execute_list.append(schedule_item)
                    else:
                        if schedule_item.t < next_time:
                            next_time = schedule_item.t
                for exec_item in execute_list:
                    if exec_item.operation != "delay":
                        root.debug("Removing {0}".format(exec_item.name))
                        self.schedule_list.remove(exec_item)
                root.debug("----------------------")
                root.debug("Schedule list:")
                for si in self.schedule_list:
                    root.debug("{0}".format(si.name))
                root.debug("----------------------")
                for exec_item in execute_list:
                    if exec_item.defer_execution is False:
                        root.debug("Executing {0} now".format(exec_item))
                        if exec_item.operation == "read":
                            self._read_attribute(exec_item.name)
                        elif exec_item.operation == "write":
                            self._write_attribute(exec_item.name, exec_item.data)
                        elif exec_item.operation == "command":
                            self._exec_command(exec_item.name, exec_item.data)
                        elif exec_item.operation == "delay":
                            root.debug("Delay timer {0} s started".format(exec_item.data))
                            exec_item.defer_execution = True
                            delay_timer = threading.Timer(exec_item.data, self._delay_cb, [exec_item])
                            delay_timer.start()

                if len(self.schedule_list) > 0:
                    root.debug("New schedule timer set: {0}".format(next_time))
                    self.schedule_timer = threading.Timer(next_time - time.time(), self.process_schedule)
                    self.schedule_timer.start()
        self.process_queue.get()

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
        return
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

    def read_attribute(self, attr_name, after_cmd=None):
        root.info("Read attribute {0}".format(attr_name))
        if after_cmd is None:
            if attr_name not in self.schedule_list:
                t = time.time()
                sch_cmd = ScheduleCommand(attr_name, t, "read")
                with self.lock:
                    bisect.insort(self.schedule_list, sch_cmd)
                self.process_schedule()
            else:
                sch_cmd = None
        else:
            sch_cmd = ScheduleCommand(attr_name, -1, "read", cmd_not_pending_list=after_cmd)
            with self.lock:
                bisect.insort(self.schedule_list, sch_cmd)
            self.process_schedule()
        return sch_cmd

    def write_attribute(self, attr_name, value, after_cmd=None):
        root.info("Write attribute {0} with {1}".format(attr_name, value))
        sch_cmd = ScheduleCommand(attr_name, -1, "write", value, cmd_not_pending_list=after_cmd)
        with self.lock:
            if attr_name not in self.schedule_list:
                # The command was not in the schedule_list, so issue a new command
                bisect.insort(self.schedule_list, sch_cmd)
            else:
                # The command was in the list, so check if it was a read or write, and modify if write
                ind = -1
                found_dup = False
                while found_dup is False:
                    try:
                        ind = self.schedule_list.index(attr_name, ind+1)
                    except ValueError:
                        break
                    if self.schedule_list[ind].operation == "write":
                        found_dup = True
                if found_dup is True:
                    self.schedule_list[ind] = sch_cmd
                else:
                    bisect.insort(self.schedule_list, sch_cmd)
        self.process_schedule()
        return sch_cmd

    def exec_command(self, cmd_name, value=None, after_cmd=None):
        root.info("Execute command {0} with {1}".format(cmd_name, value))
        sch_cmd = ScheduleCommand(cmd_name, -1, "command", value, cmd_not_pending_list=after_cmd)
        with self.lock:
            if cmd_name not in self.schedule_list:
                # The command was not in the schedule_list, so issue a new command
                bisect.insort(self.schedule_list, sch_cmd)
            else:
                # The command was in the list, so check if it was exec, and modify if so
                ind = -1
                found_dup = False
                while found_dup is False:
                    try:
                        ind = self.schedule_list.index(cmd_name, ind + 1)
                    except ValueError:
                        break
                    if self.schedule_list[ind].operation == "command":
                        found_dup = True
                if found_dup is True:
                    self.schedule_list[ind] = sch_cmd
                else:
                    bisect.insort(self.schedule_list, sch_cmd)
        self.process_schedule()
        return sch_cmd

    def delay_command(self, value, after_cmd=None):
        root.info("Adding delay command {0} s".format(value))
        cmd_name = "delay"
        t = time.time()
        if after_cmd is None:
            if cmd_name not in self.schedule_list:
                sch_cmd = ScheduleCommand(cmd_name, t, "delay", value)
                with self.lock:
                    bisect.insort(self.schedule_list, sch_cmd)
                self.process_schedule()
        else:
            sch_cmd = ScheduleCommand(cmd_name, t, "delay", value, cmd_not_pending_list=after_cmd)
            with self.lock:
                bisect.insort(self.schedule_list, sch_cmd)
            self.process_schedule()
        return sch_cmd

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
    roi = [0, 0, 1394, 1040]
    cmd0 = cam.exec_command("stop")
    cmd_d = cam.delay_command(0.5, after_cmd=cmd0)
    cmd1 = cam.write_attribute("imageoffsetx", roi[0], after_cmd=cmd_d)
    cam.exec_command("start", after_cmd=[cmd1])

