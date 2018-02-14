# -*- coding:utf-8 -*-
"""
Created on Feb 06, 2018

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

root = logging.getLogger("DeviceController")
while len(root.handlers):
    root.removeHandler(root.handlers[0])

f = logging.Formatter("%(asctime)s - %(module)s.   %(funcName)s - %(levelname)s - %(message)s")
fh = logging.StreamHandler()
fh.setFormatter(f)
root.addHandler(fh)
root.setLevel(logging.ERROR)


class Condition(object):
    def __init__(self, cond_obj, valid_range=None, invalid_range=None, range_type=None):
        """

        :param cond_obj: Object whose condition is to be checked. Must have a get_value method
        :param valid_range: Range of values that is valid. If numeric range, this must be a 2 element list with
        [low boundary, high boundary]. If enumeration range, this must be a list of valid values.
        :param invalid_range: Range of values that is invalid. If numeric range, this must be a 2 element list with
        [low boundary, high boundary]. If enumeration range, this must be a list of invalid values.
        :param range_type: Type of the range. Must be "range" for numeric range, "enum" for enumeration type, or None
        """
        self.cond_obj = cond_obj
        self.valid_range = valid_range
        self.invalid_range = invalid_range

        self.range_type = range_type
        if self.range_type is None:
            self._determine_range_type()

        self.fulfilled = False

    def check_condition(self):
        result = False
        try:
            attr = self.cond_obj.get_attribute()
        except NameError:
            self.fulfilled = False
            return False
        if attr is None:
            self.fulfilled = True
            return True
        if type(attr) is not pt.DeviceAttribute:
            root.error("Type attr: {0}".format(type(attr)))
        if attr.quality is pt.AttrQuality.ATTR_VALID:
            if self.range_type is not None:
                if self.range_type == "range":
                    if self.valid_range is not None:
                        result = self.valid_range[0] < attr.value < self.valid_range[1]
                    if self.invalid_range is not None:
                        result = not self.invalid_range[0] < attr.value < self.invalid_range[1]
                elif self.range_type == "enum":
                    if self.valid_range is not None:
                        result = attr.value in self.valid_range
                    if self.invalid_range is not None:
                        result = attr.value not in self.invalid_range
            else:
                result = True
        else:
            result = False
        self.fulfilled = result
        return result

    def get_status(self):
        return self.fulfilled

    def _determine_range_type(self):
        if self.valid_range is not None:
            if type(self.valid_range) is not list:
                self.valid_range = [self.valid_range]
            if type(self.valid_range[0]) in [pt._PyTango.DevState, pt._PyTango.AttrQuality]:
                self.range_type = "enum"
            elif len(self.valid_range) != 2:
                self.range_type = "enum"
            else:
                self.range_type = "range"
        elif self.invalid_range is not None:
            if type(self.invalid_range) is not list:
                self.invalid_range = [self.invalid_range]
            if type(self.invalid_range[0]) in [pt._PyTango.DevState, pt._PyTango.AttrQuality]:
                self.range_type = "enum"
            elif len(self.invalid_range) != 2:
                self.range_type = "enum"
            else:
                self.range_type = "range"
        else:
            self.range_type = None


class DeviceCommand(object):
    def __init__(self, name, operation, device, data=None, recurrent=False, period=1.0, timeout=2.0):
        self.name = name
        self.operation = operation
        self.device = device
        self.data = data

        self.done = False
        self.pending = False
        self.timed_out = False
        self.status_msg = ""
        self.state = pt.DevState.ON

        self.subscriber_list = list()
        self.subscriber_lock = threading.Lock()
        self.condition_dict = dict()
        self.post_action_list = list()

        self.recurrent = recurrent
        self.period = period
        self.delay_timer = None
        self.start_time = time.time()
        self.attr_result = None
        self._timeout = timeout
        self.timeout_timer = None

    def add_subscriber(self, subscriber):
        with self.subscriber_lock:
            if subscriber not in self.subscriber_list:
                self.subscriber_list.append(subscriber)

    def remove_subscriber(self, subscriber):
        with self.subscriber_lock:
            if subscriber in self.subscriber_list:
                self.subscriber_list.remove(subscriber)

    def notify_subscribers(self):
        remove_list = list()
        with self.subscriber_lock:
            for subscriber in self.subscriber_list:
                try:
                    root.debug("Calling subscriber {0}".format(subscriber))
                    subscriber(self)
                except NameError:
                    remove_list.append(subscriber)
                except Exception as e:
                    root.error("Error notifying subscriber {0} returned {1}".format(subscriber, str(e)))
        for rem_sub in remove_list:
            self.remove_subscriber(rem_sub)

    def start(self):
        root.debug("Starting command \"{0}\"".format(self.name))
        try:
            self.delay_timer.cancel()
        except AttributeError:
            pass
        try:
            self.timeout_timer.cancel()
        except AttributeError:
            pass
        self.pending = True
        self.done = False
        self.timed_out = False
        self.start_time = time.time()
        if self._timeout is not None:
            self.timeout_timer = threading.Timer(self._timeout, self._timeout_cb)
            self.timeout_timer.start()
        self.status_msg = "Command started"
        self.state = pt.DevState.ON
        self.check_condition()

    def cancel(self):
        root.debug("Cancelling command \"{0}\"".format(self.name))
        self.pending = False
        try:
            self.delay_timer.cancel()
        except AttributeError:
            pass
        try:
            self.timeout_timer.cancel()
        except AttributeError:
            pass
        self.clear_conditions()
        self.status_msg = "Command cancelled"

    def check_condition(self, cond_obj=None):
        root.debug("Check conditions for \"{0}\"".format(self.name))
        remove_list = []
        if cond_obj is None:
            for cond in self.condition_dict:
                try:
                    if cond.done is True:
                        result = self.condition_dict[cond].check_condition()
                        root.debug("DeviceCommand \"{0}\" checking condition \"{1}\": {2}".format(self.name,
                                                                                                  cond.name,
                                                                                                  result))
                        # if result is True:
                        #     remove_list.append(cond)
                except NameError:
                    remove_list.append(cond)
        else:
            try:
                root.debug("Condition status: {0}".format(cond_obj.done))
                if cond_obj.done is True and cond_obj in self.condition_dict:
                    result = self.condition_dict[cond_obj].check_condition()
                    root.debug("DeviceCommand \"{0}\" checking condition \"{1}\": {2}".format(self.name,
                                                                                              cond_obj.name,
                                                                                              result))
                    # if result is True:
                    #     remove_list.append(cond_obj)
            except NameError:
                remove_list.append(cond_obj)
        for remove_cond in remove_list:
            self.condition_dict.pop(remove_cond)
        fulfilled = True
        for cond in self.condition_dict:
            if self.condition_dict[cond].get_status() is False:
                fulfilled = False
                break
        if fulfilled is True:
            self.execute_operation()

    def add_condition(self, cond_obj, valid_range=None, invalid_range=None):
        if cond_obj not in self.condition_dict:
            new_cond = Condition(cond_obj, valid_range, invalid_range)
            self.condition_dict[cond_obj] = new_cond
            cond_obj.add_subscriber(self.check_condition)

    def clear_conditions(self):
        self.condition_dict = dict()

    def get_attribute(self):
        return self.attr_result

    def execute_operation(self):
        root.debug("Starting execution of operation {0} for \"{1}\", pending {2}".format(self.operation.upper(),
                                                                                         self.name, self.pending))
        if self.pending is True:
            if self.operation == "read":
                self._read_attribute()
            elif self.operation == "write":
                self._write_attribute()
            elif self.operation == "command":
                self._exec_command()
            elif self.operation == "delay":
                self._delay_operation()

    def exec_post_actions(self):
        root.debug("Executing post actions for {0}".format(self.name))
        self.pending = False
        if self.state == pt.DevState.ON:
            self.done = True
            self.status_msg = "Command finished"
        else:
            self.done = False
        for action in self.post_action_list:
            action.execute()
        self.notify_subscribers()

    def _read_attribute(self):
        root.info("Sending read attribute \"{0}\" to device".format(self.name))
        if self.device is not None:
            try:
                attr_future = self.device.read_attribute(self.name, wait=False)
            except pt.DevFailed as e:
                root.error("read_attribute returned error {0}".format(str(e)))
                self.status_msg = e[0].desc
                return False
            attr_future.add_done_callback(self._attribute_cb)

    def _write_attribute(self):
        if self.device is not None:
            try:
                attr_future = self.device.write_attribute(self.name, self.data, wait=False)
            except pt.DevFailed as e:
                root.error("write_attribute returned error {0}".format(str(e)))
                self.status_msg = e[0].desc
                return False
            attr_future.add_done_callback(self._attribute_cb)

    def _exec_command(self):
        if self.device is not None:
            try:
                cmd_future = self.device.command_inout(self.name, self.data, wait=False)
            except pt.DevFailed as e:
                root.error("exec_command returned error {0}".format(str(e)))
                self.status_msg = e[0].desc
                return False
            cmd_future.add_done_callback(self._attribute_cb)

    def _delay_operation(self):
        root.debug("Delay timer {0} s started".format(self.data))
        try:
            if self.delay_timer.is_alive() is True:
                self.delay_timer.cancel()
        except AttributeError:
            pass
        self.delay_timer = threading.Timer(self.data, self._attribute_cb)
        self.delay_timer.start()

    def _attribute_cb(self, attr_future=None):
        root.info("\"{0}\" _attribute callback".format(self.name))
        if attr_future is not None:
            if attr_future.cancelled() is True:
                root.error("Attribute future cancelled")
                return
            try:
                attr = attr_future.result()
            except pt.DevFailed as e:
                root.error("Attribute future devfailed {0}".format(str(e)))
                root.error("origin {0}".format(e[0].origin))
                root.error("reason {0}".format(e[0].reason))
                attr = None
                if e[0].reason == "API_DeviceNotExported":
                    self.state = pt.DevState.UNKNOWN
                    # self.device = None
                    self.status_msg = e[0].desc
                    # return
                elif e[0].reason == "API_DeviceTimedOut":
                    self.state = pt.DevState.UNKNOWN
                    # self.device = None
                    self.status_msg = e[0].desc
                    # return
                elif e[0].reason == "API_CantConnectToDevice":
                    self.state = pt.DevState.UNKNOWN
                    # self.device = None
                    self.status_msg = e[0].desc
                    # return
                elif e[0].reason == "DB_DeviceNotDefined":
                    self.state = pt.DevState.UNKNOWN
                    # self.device = None
                    self.status_msg = e[0].desc
                    # return
                elif e[0].reason == "API_AttrNotAllowed":
                    self.state = pt.DevState.ALARM
                    # self.device = None
                    self.status_msg = e[0].desc
                    # return
                else:
                    root.error("Re-throw")
                    pt.Except.re_throw_exception(e, "", "", "")
            except ValueError as e:
                root.error("Value error for future {0}".format(e))
                self.status_msg = "Value error for device concurrent callback"
                return
        else:
            attr = None
        self.attr_result = attr     # Save result
        if attr is not None:
            root.debug("Attribute \"{0}\" result received".format(attr.name))
            self.status_msg = "Attribute \"{0}\" result received".format(attr.name)
        self.exec_post_actions()

    def _timeout_cb(self):
        root.debug("------------------------------------------------------------")
        root.debug("Timeout for command \"{0}\"".format(self.name))
        root.debug("------------------------------------------------------------")
        self.cancel()
        self.pending = False
        self.done = False
        self.timed_out = True
        self.timeout_timer = None
        self.status_msg = "Command timeout"
        self.state = pt.DevState.ALARM
        self.notify_subscribers()


class AttributeWrapper(object):
    def __init__(self, name, attr=None):
        self.name = name
        if attr is None:
            self.attr_result = pt.DeviceAttribute()
        else:
            self.attr_result = attr
        self.done = True

        self.subcriber_lock = threading.Lock()
        self.subscriber_list = list()

    def add_subscriber(self, subscriber):
        with self.subcriber_lock:
            if subscriber not in self.subscriber_list:
                self.subscriber_list.append(subscriber)

    def remove_subscriber(self, subscriber):
        with self.subcriber_lock:
            if subscriber in self.subscriber_list:
                self.subscriber_list.remove(subscriber)

    def notify_subscribers(self):
        remove_list = list()
        with self.subcriber_lock:
            for subscriber in self.subscriber_list:
                try:
                    subscriber(self)
                except NameError:
                    remove_list.append(subscriber)
                except Exception as e:
                    root.error("Error notifying subscriber {0} returned {1}".format(subscriber, str(e)))
        for rem_sub in remove_list:
            self.remove_subscriber(rem_sub)

    def get_attribute(self):
        return self.attr_result

    def set_attribute(self, attr):
        if type(attr) in [DeviceCommand, AttributeWrapper]:
            self.attr_result = attr.get_attribute()
        else:
            self.attr_result = attr
        root.debug("set attribute \"{0}\" to value {1}".format(self.name, self.attr_result.value))
        self.notify_subscribers()


if __name__ == "__main__":
    dev = ptf.DeviceProxy("sys/tg_test/1")
    state_dc = DeviceCommand("state", "read", dev, recurrent=False, period=1.0)
    state_at = AttributeWrapper("state")
    state_dc.add_subscriber(state_at.set_attribute)
    delay_dc = DeviceCommand("delay01", "delay", dev, 2.0)
    state_dc.add_condition(delay_dc)
    gain_dc = DeviceCommand("double_scalar", "read", dev)
    gain_dc.add_condition(state_at, invalid_range=[pt.DevState.UNKNOWN, pt.DevState.FAULT])
    state_dc.start()
    delay_dc.start()
    gain_dc.start()
