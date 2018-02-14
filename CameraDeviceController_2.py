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
    """
    Class for handling states in a camera device. A command list contains commands that are issued and
    awaiting result.

    Will issue commands setting up an initial state upon entering INIT state. Will also setup a set of
    polled attributes, such as image.

    Command results are processed in device_command_cb, where recurrend (polled) commands are re-issued.

    If a state change is detected camera_state_cb is called and the internal state is set accordingly.
    External classes, such as a DeviceServer, can subscribe to state changes, whereby a stored method is called.
    """
    def __init__(self, device_name, parameter_dict=None):
        self.device_name = device_name
        self.device = None

        self.lock = threading.Lock()
        self.state_condition_var = threading.Condition()
        self.wakeup_condition_var = threading.Condition()
        self.watchdog_timer = None
        self.watchdog_timeout = 10.0
        self.state = pt.DevState.UNKNOWN
        self.status_msg = ""
        self.state_cb_list = list()

        self.attribute_dict = dict()
        self.command_list = list()
        self.parameter_dict = dict()
        self.setup_parameters(parameter_dict)

        self.state_thread = threading.Thread()
        threading.Thread.__init__(self.state_thread, target=self.statehandler_dispatcher)

        self.command_queue = Queue.Queue(100)

        self.statehandler_dict = {pt.DevState.ON: self.on_handler,
                                  pt.DevState.STANDBY: self.on_handler,
                                  pt.DevState.RUNNING: self.on_handler,
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
        """
        Handler for UNKNOWN state: connecting to camera (first disconnecting).
        Connection is verified by sending read state command.
        :param prev_state:
        :return:
        """
        root.debug("Entering unknown_handler")
        connection_timeout = 1.0

        while self.get_state() == pt.DevState.UNKNOWN and self.stop_state_thread_flag is False:
            with self.state_condition_var:
                self.connect()
                self.state_condition_var.wait(3.0)
            if self.device is not None:
                state_cmd = dc.DeviceCommand("state", "read", self.device, timeout=4.0)
                state_cmd.add_subscriber(self.unknown_connect_ready)
                state_cmd.start()
                with self.wakeup_condition_var:
                    self.wakeup_condition_var.wait(4.0)
                if state_cmd.done is True:
                    self.set_state(pt.DevState.INIT)
            else:
                with self.wakeup_condition_var:
                    self.wakeup_condition_var.wait(connection_timeout)

    def init_handler(self, prev_state):
        """
        Handler for INIT state: setting up initial conditions by stopping camera, then issuing
        commands in a parameter_dict. After that the camera is started again.

        Setting up polled attributes in the command_list
        """
        root.debug("Entering init_handler")
        timeout = 7.0

        root.debug("Setting up initial parameters")
        root.debug("command list length: {0}".format(len(self.command_list)))
        with self.lock:
            stop_cmd = dc.DeviceCommand("stop", "command", self.device)
            stop_cmd.start()
            stop_delay_cmd = dc.DeviceCommand("stop_delay", "delay", self.device, 0.5)
            stop_delay_cmd.add_condition(stop_cmd)
            stop_delay_cmd.start()
            prev_cmd = stop_cmd
            for param in self.parameter_dict:
                root.debug("Parameter \"{0}\": {1}".format(param, self.parameter_dict[param]))
                cmd = dc.DeviceCommand(param, "write", self.device, self.parameter_dict[param])
                cmd.add_condition(stop_delay_cmd)
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
            self.command_list.append(start_cmd)
            root.debug("Starting start command")
            start_cmd.start()
            root.debug("command list length: {0}".format(len(self.command_list)))

        self.reset_watchdog()

        with self.wakeup_condition_var:
            root.debug("Waiting for condition variable")
            self.wakeup_condition_var.wait(timeout)

        if start_cmd.done is True:
            root.info("Setting up periodic read attributes dict")
            self.add_polled_attribute("state", 0.25, subscriber_method=self.camera_state_cb)
            self.add_polled_attribute("gain", 2.0)
            self.add_polled_attribute("exposuretime", 2.0)
            self.add_polled_attribute("image", 0.1)
            self.set_state(pt.DevState.ON)
        else:
            with self.lock:
                for cmd in self.command_list:
                    cmd.cancel()
            self.command_list = []

    def on_handler(self, prev_state):
        """
        Handler for ON, RUNNNING, STANDBY, and ALARM states.
        All actual processing is done in the device_command_cb callback.

        Additional commands can be set by external class using write_attribute, read_attribute, and
        exec_command methods.

        :param prev_state:
        :return:
        """
        root.debug("Entering on_handler")
        timeout = 0.2
        handled_states = [pt.DevState.ON, pt.DevState.RUNNING, pt.DevState.STANDBY, pt.DevState.ALARM]
        while self.get_state() in handled_states and self.stop_state_thread_flag is False:
            with self.state_condition_var:
                self.state_condition_var.wait(timeout)

    def off_handler(self, prev_state):
        root.debug("Entering off_handler")
        timeout = 0.2
        handled_states = [pt.DevState.OFF]
        while self.get_state() in handled_states and self.stop_state_thread_flag is False:
            with self.state_condition_var:
                self.state_condition_var.wait(timeout)

    def running_handler(self, prev_state):
        pass

    def standby_handler(self, prev_state):
        pass

    def fault_handler(self, prev_state):
        """
        Handler for FAULT state.

        Additional fault handling logic should be added here.

        :param prev_state:
        :return:
        """
        root.debug("Entering fault_handler")
        timeout = 0.2
        handled_states = [pt.DevState.FAULT]
        while self.get_state() in handled_states and self.stop_state_thread_flag is False:
            with self.state_condition_var:
                self.state_condition_var.wait(timeout)

    def connect(self):
        """
        Disconnect from camera  by cancelling all outstanding commands.
        Then create new deviceproxy object.

        :return:
        """
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
        """
        Callback for device proxy creation. If there is an error, the state is set to UNKNOWN.
        Normally errors are not detected here.
        :param dev_future:
        :return:
        """
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
                    self.set_status("Device not started")
                    self.set_state(pt.DevState.UNKNOWN)
                    dev = None
                elif e[0].reason == "DB_DeviceNotDefined":
                    self.set_status("Device not found in database")
                    self.set_state(pt.DevState.UNKNOWN)
                    dev = None
                else:
                    raise
        self.device = dev
        with self.state_condition_var:
            self.state_condition_var.notify_all()

    def reset_watchdog(self):
        """
        Watchdog to detect a dead camera device.
        This method must be called periodically to reset a timer.

        :return:
        """
        if self.watchdog_timer is not None:
            self.watchdog_timer.cancel()
        self.watchdog_timer = threading.Timer(self.watchdog_timeout, self.watchdog_handler)
        self.watchdog_timer.start()

    def stop_watchdog(self):
        if self.watchdog_timer is not None:
            self.watchdog_timer.cancel()

    def watchdog_handler(self):
        """
        If the watchdog timer times out this method is invoked.
        Only sets the state to UNKNOWN for now.

        :return:
        """
        root.debug("Watchdog timed out. ")
        self.set_state(pt.DevState.UNKNOWN)

    def get_attribute(self, attr_name):
        """
        Retrieve attribute result.

        Attribute results are stored in a dictionary. If the desired attribute is not in the
        dictionary, None is returned.

        :param attr_name: Name of attribute to retrieve
        :return: DeviceAttribute (None if there is no attribute with that name)
        """
        with self.lock:
            if attr_name in self.attribute_dict:
                attr = self.attribute_dict[attr_name]
            else:
                attr = None
        return attr

    def write_attribute(self, attr_name, value):
        """
        Write attribute value to camera device. Issues three commands:
        write_attribute
        delay 0.1 s
        read_attribute

        :param attr_name: Name of the command
        :param value: Value to write to the command
        :return:
        """
        root.debug("Adding Write_attribute \"{0}\" to {1}".format(attr_name, value))
        with self.lock:
            prev_cmd = dc.DeviceCommand(attr_name, "write", self.device, value)
            prev_cmd.add_subscriber(self.device_command_cb)
            prev_cmd.start()
            self.command_list.append(prev_cmd)
            cmd_d = dc.DeviceCommand("delay_w", "delay", self.device, 0.1)
            cmd_d.add_condition(prev_cmd)
            cmd_d.start()
            cmd = dc.DeviceCommand(attr_name, "read", self.device)
            cmd.add_condition(cmd_d)
            cmd.add_subscriber(self.device_command_cb)
            cmd.start()
            self.command_list.append(cmd)
        root.debug("command list length: {0}".format(len(self.command_list)))

    def exec_command(self, cmd_name, data=None):
        """
        Execute a command on the camera device. No additional commands are sent.

        :param cmd_name: Name of the command (e.g. start)
        :param data: Optional data used by the command
        :return:
        """
        with self.lock:
            cmd = dc.DeviceCommand(cmd_name, "command", self.device, data)
            cmd.start()

    def add_polled_attribute(self, attr_name, period, subscriber_method=None):
        """
        Add a polled read attribute to the command_list. This is done by setting the recurrent option
        to True.

        :param attr_name: Name of the attribute
        :param period: Polling period
        :param subscriber_method: Optional callback method to call when the the attribute is done. Additionally
        device_command_cb is added as a subscriber.
        :return:
        """
        root.info("Adding attribute {0} with polling period {1} s".format(attr_name, period))
        with self.lock:
            attr = pt.DeviceAttribute()
            self.attribute_dict[attr_name] = attr
            dev_cmd = dc.DeviceCommand(attr_name, "read", self.device, recurrent=True, period=period)
            if subscriber_method is not None:
                dev_cmd.add_subscriber(subscriber_method)
            self.command_list.append(dev_cmd)
            dev_cmd.add_subscriber(self.device_command_cb)
            dev_cmd.start()

    def get_state(self):
        """
        Return the current internal state.

        :return:
        """
        return self.state

    def set_state(self, new_state):
        """
        Set internal state. Call list of subscribers, including the state and the status message.

        :param new_state:
        :return:
        """
        root.debug("Setting new state {0}".format(new_state))
        self.state = new_state
        self.set_status("")
        for cb in self.state_cb_list:
            root.debug("Calling state callback {0}".format(cb))
            cb(new_state, self.get_status())
        with self.state_condition_var:
            self.state_condition_var.notify_all()

    def get_status(self):
        return self.status_msg

    def set_status(self, status_msg, append=False):
        if append is True:
            self.status_msg += "\n" + status_msg
        else:
            self.status_msg = "{0}\n\n{1}".format(self.get_state(), status_msg)

    def add_state_callback(self, cb):
        root.debug("Adding state callback {0}".format(cb))
        self.state_cb_list.append(cb)

    def disconnect(self):
        """
        Disconnect from device proxy. Cancel watchdog timer and command list. Set state to UNKNOWN.
        :return:
        """
        try:
            self.watchdog_timer.cancel()
        except AttributeError:
            pass
        with self.lock:
            for cmd in self.command_list:
                cmd.cancel()
            self.command_list = []
        self.device = None
        self.set_state(pt.DevState.UNKNOWN)

    def stop_thread(self):
        """
        Stop state handler thread by setting stop flag. Disconnect from device proxy.
        :return:
        """
        self.stop_state_thread_flag = True
        with self.wakeup_condition_var:
            self.wakeup_condition_var.notify_all()
        self.disconnect()

    def setup_parameters(self, parameter_dict):
        """
        Setup initial parameter dict. If the state is not UNKNOWN, set state to INIT
        to ensure that the parameter dict is loaded to the camera.
        :param parameter_dict:
        :return:
        """
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
        """
        Callback method for when the initial parameters are set. Wakes up the condition variable
        that the init_handler is waiting for.
        :param cmd:
        :return:
        """
        root.debug("init_parameters_ready called")
        with self.wakeup_condition_var:
            self.wakeup_condition_var.notify_all()

    def unknown_connect_ready(self, cmd):
        """
        Callback method for when connecting to camera and sending read state. Wakes up the condition variable
        that the unknown_handler is waiting for.
        :param cmd:
        :return:
        """
        root.debug("unknown_connect_ready called")
        if cmd.done is True:
            with self.wakeup_condition_var:
                self.wakeup_condition_var.notify_all()

    def camera_state_cb(self, cmd):
        """
        Callback method for read state polling attribute. If the camera is not RUNNING a start command is sent.
        :param cmd:
        :return:
        """
        camera_state = cmd.get_attribute().value
        root.debug("camera_state_cb called, state {0}".format(camera_state))
        if camera_state in [pt.DevState.ON, pt.DevState.STANDBY]:
            start_queued = False
            with self.lock:
                for cmd in self.command_list:
                    if cmd.name == "start":
                        root.debug("Found start command in list")
                        start_queued = True
                if start_queued is False:
                    root.debug("Issuing start command")
                    start_cmd = dc.DeviceCommand("start", "command", self.device)
                    start_cmd.start()
                    self.command_list.append(start_cmd)
        if self.get_state() is not camera_state:
            self.set_state(camera_state)

    def device_command_cb(self, cmd_d):
        """
        Callback method for attributes in the command_list. If the command had an error this is read out
        and the internal state is set accordingly.

        Recurrent commands are re-issued.

        Watchdog is reset.

        :param cmd_d:
        :return:
        """
        remove_list = list()
        reset_flag = False
        new_state = None
        with self.lock:
            root.debug("\"{0}\" device_command_cb: \"{1}\" {2}".format(self.device_name, cmd_d.name, cmd_d.operation.upper()))
            root.debug("command list length: {0}".format(len(self.command_list)))
            for cmd in self.command_list:
                if cmd.timed_out is True:
                    root.debug("Command \"{0}\" timed out".format(cmd.name))
                    remove_list.append(cmd)
                    self.set_status(cmd.status_msg)
                if cmd.done is True:
                    remove_list.append(cmd)
                    if cmd.operation == "read":
                        self.attribute_dict[cmd.name] = cmd.attr_result
                    reset_flag = True
                else:
                    if cmd.state is pt.DevState.UNKNOWN:
                        new_state = pt.DevState.UNKNOWN
                        new_status = cmd.status_msg
                    elif cmd.state is pt.DevState.ALARM:
                        new_state = pt.DevState.ALARM
                        new_status = cmd.status_msg

            root.debug("Removing {0} commands".format(len(remove_list)))
            for cmd_r in remove_list:
                try:
                    self.command_list.remove(cmd_r)
                except ValueError:
                    # The command was not in the list, just ignore it
                    pass
                if cmd_r.recurrent is True:
                    t = cmd_r.start_time + cmd_r.period - time.time()
                    delay_cmd = dc.DeviceCommand("delay_{0}".format(cmd_r.name), "delay", self.device, t)
                    cmd_r.add_condition(delay_cmd)
                    delay_cmd.start()
                    cmd_r.start()
                    self.command_list.append(cmd_r)
        if reset_flag is True:
            root.debug("Resetting watchdog")
            self.reset_watchdog()
        if new_state is not None:
            self.set_status(new_status, append=True)
            self.set_state(new_state)

    def __del__(self):
        root.debug("Delete object for \"{0}\"".format(self.device_name))
        self.disconnect()

if __name__ == "__main__":
    params = dict()
    params["imageoffsetx"] = 0
    params["imageoffsety"] = 0
    params["imageheight"] = 300
    params["imagewidth"] = 1000
    params["triggermode"] = "Off"
    cdc = CameraDeviceController("gunlaser/cameras/blackfly_test01", params)
