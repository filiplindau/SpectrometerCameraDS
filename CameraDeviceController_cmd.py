# -*- coding:utf-8 -*-
"""
Created on Feb 02, 2018

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


class CommandCondition(object):
    def __init__(self, name):
        self.name = name
        self._subscriber_list = []
        self.is_fired = False

    def eval(self, t, cmd_list=None, state=None):
        return True

    def add_subscriber(self, subscriber_callable):
        if subscriber_callable not in self._subscriber_list:
            self._subscriber_list.append(subscriber_callable)

    def fire_condition(self):
        if self.is_fired is False:
            self.is_fired = True
            for subscriber in self._subscriber_list:
                subscriber(self.name)

    def _reset(self):
        self.is_fired = False

    def __eq__(self, other):
        if type(other) == str:
            return self.name == other
        else:
            return self.name == other.name


class TimeCondition(CommandCondition):
    def __init__(self, name, scheduled_time):
        CommandCondition.__init__(self, name)
        self.scheduled_time = None
        self.timer = None
        self.restart(scheduled_time)

    def restart(self, scheduled_time):
        try:
            if self.timer.is_alive() is True:
                self.timer.cancel()
        except AttributeError:
            pass
        self._reset()
        self.scheduled_time = scheduled_time
        t = scheduled_time - time.time()
        self.timer = threading.Timer(t, self.fire_condition)
        self.timer.start()

    def eval(self, t, cmd_list=None, state=None):
        result = t > self.scheduled_time
        return result


class StateCondition(CommandCondition):
    def __init__(self, name, invalid_states=[], valid_states=[]):
        CommandCondition.__init__(self, name)
        self.invalid_states = invalid_states
        self.valid_states = valid_states

    def eval(self, t, cmd_list=None, state=None):
        result = False
        # Check if the valid_states list is empty:
        if self.valid_states:
            # No, so check if the state is in valid list:
            if state in self.valid_states:
                result = True
        else:
            # Yes, it was empty so assume the state is valid until found in the invalid list
            result = True
        # Check if the state is in the invalid list. Works even if the list is empty.
        if state in self.invalid_states:
            result = False
        if result is True:
            self.fire_condition()
        return result


class AwaitCommandCondition(CommandCondition):
    def __init__(self, name, cmd_list):
        CommandCondition.__init__(self, name)
        if type(cmd_list) is not list:
            cmd_list = [cmd_list]
        self.cmd_list = cmd_list

    def eval(self, t, cmd_list=None, state=None):
        result = True
        for cmd in cmd_list:
            if cmd in self.cmd_list:
                result = False
                break
        return result


class DeviceCommand(object):
    def __init__(self, name, device, operation="read", scheduled_time=None, data=None, recurrent=False, period=1.0):
        self.name = name
        self.device = device
        self.operation = operation
        self.scheduled_time = scheduled_time
        self.data = data
        self.attr_result = None

        self.done = False
        self.execute_conditions_list = []
        self.post_actions = []
        self.recurrent = recurrent
        self.period = period
        self.timer = None

        self.subscriber_list = []

    def init_conditions(self):
        if self.scheduled_time is not None:
            time_cond = TimeCondition("time", self.scheduled_time, self.condition_ready)
            self.execute_conditions_list.append(time_cond)

    def add_condition(self, condition):
        self.execute_conditions_list.append(condition)
        condition.add_subscriber(self.condition_ready)

    def condition_ready(self, condition_name):
        self.eval_exec_conditions(time.time(), state=self.state)

    def eval_exec_conditions(self, t, cmd_list=None, state=None):
        result = True
        for condition in self.execute_conditions_list:
            try:
                tmp_result = condition.eval(t, cmd_list, state)
            except ValueError:
                result = False
                break
            if tmp_result is False:
                result = False
                break
        if result is True and self.done is False:
            self.exec_operation()
        return result

    def exec_operation(self):
        root.debug("Starting execution of operation {0} for {1}".format(self.operation, self.name))
        if self.operation == "read":
            self._read_attribute()
        elif self.operation == "write":
            self._write_attribute()
        elif self.operation == "command":
            self._exec_command()
        elif self.operation == "delay":
            self._delay_operation()

    def exec_post_actions(self):
        for action in self.post_actions:
            action.exec()
        for subscriber in self.subscriber_list:
            subscriber(self.name, self.attr_result)

    def _read_attribute(self):
        root.info("Sending read attribute {0} to device".format(self.name))
        if self.device is not None:
            try:
                attr_future = self.device.read_attribute(self.name, wait=False)
            except pt.DevFailed as e:
                root.error("read_attribute returned error {0}".format(str(e)))
                return False
            attr_future.add_done_callback(self._attribute_cb)

    def _write_attribute(self):
        if self.device is not None:
            try:
                attr_future = self.device.write_attribute(self.name, self.data, wait=False)
            except pt.DevFailed as e:
                root.error("write_attribute returned error {0}".format(str(e)))
                return False
            attr_future.add_done_callback(self._attribute_cb)

    def _exec_command(self):
        if self.device is not None:
            try:
                cmd_future = self.device.command_inout(self.name, self.data, wait=False)
            except pt.DevFailed as e:
                root.error("exec_command returned error {0}".format(str(e)))
                return False
            cmd_future.add_done_callback(self._attribute_cb)

    def _delay_operation(self):
        root.debug("Delay timer {0} s started".format(self.data))
        try:
            if self.timer.is_alive() is True:
                self.timer.cancel()
        except AttributeError:
            pass
        self.timer = threading.Timer(self.data, self._attribute_cb)
        self.timer.start()

    def _attribute_cb(self, attr_future=None):
        root.info("_attribute callback")
        if attr_future is not None:
            if attr_future.cancelled() is True:
                root.error("Attribute future cancelled")
                return
            try:
                attr = attr_future.result()
                root.debug("Attribute {0} result received".format(attr.name))
            except pt.DevFailed as e:
                root.error("Attribute future devfailed {0}".format(str(e)))
                root.error("origin {0}".format(e[0].origin))
                root.error("reason {0}".format(e[0].reason))
                if e[0].reason == "API_DeviceNotExported":
                    self.state = pt.DevState.UNKNOWN
                    self.device = None
                    return
                elif e[0].reason == "API_DeviceTimedOut":
                    self.state = pt.DevState.UNKNOWN
                    self.device = None
                    return
                elif e[0].reason == "API_CantConnectToDevice":
                    self.state = pt.DevState.UNKNOWN
                    self.device = None
                    return
                else:
                    root.error("Re-throw")
                    pt.Except.re_throw_exception(e, "", "", "")
            except ValueError as e:
                root.error("Value error for future {0}".format(e))
                return
        else:
            attr = None
        self.attr_result = attr     # Save result
        if self.recurrent is True:
            new_time = self.scheduled_time + self.period
            self.scheduled_time = new_time
            self.done = False
        else:
            self.done = True
        self.exec_post_actions()

    def get_attribute(self):
        return self.attr_result

    def add_subscriber(self, subscriber_callable):
        self.subscriber_list.append(subscriber_callable)

    def remove_subscriber(self, subscriber_callable):
        try:
            self.subscriber_list.remove(subscriber_callable)
        except ValueError:
            pass


class CameraDeviceController(object):
    def __init__(self, device_name):
        self.device_name = device_name
        self.device = None

        self.lock = threading.Lock()

        self.watchdog_timer = None
        self.watchdog_timeout = 3.0
        self.state = pt.DevState.UNKNOWN
        self.state_cb_list = []

        self.command_list = []

    def connect(self):
        root.info("Connecting to {0}".format(self.device_name))
        if self.device is not None:
            self.disconnect()

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
            return
        try:
            dev = dev_future.result()
        except pt.DevFailed as e:
            root.error("Device future devfailed {0}".format(str(e)))
            if e[0].reason == "API_DeviceNotExported":
                self.set_state(pt.DevState.UNKNOWN)
                self.device = None
                return
            else:
                raise
        self.device = dev
        state_cmd = DeviceCommand("state", self.device, "read", recurrent=True, period=0.5)
        self.add_command(state_cmd)
        # self.set_state(pt.DevState.ON)
        # self.reset_watchdog()

    def disconnect(self):
        if self.device is not None:
            self.device = None
        self.set_state(pt.DevState.UNKNOWN)

    def set_state(self, new_state):
        root.debug("Setting new state {0}".format(new_state))
        self.state = new_state
        for cb in self.state_cb_list:
            root.debug("Calling state callback {0}".format(cb))
            cb(new_state)

    def add_command(self, cmd):
        self.command_list.append(cmd)


if __name__ == "__main__":
    # devcon = CameraDeviceController("gunlaser/cameras/blackfly_test01")
    dev = pt.DeviceProxy("gunlaser/cameras/blackfly_test01")
    devcmd = DeviceCommand("state", dev, "read", time.time()+2)
