# -*- coding:utf-8 -*-
"""
Created on Apr 13, 2018

@author: Filip Lindau

All data is in the controller object. The state object only stores data needed to keep track
of state progress, such as waiting deferreds.
When a state transition is started, a new state object for that state is instantiated.
The state name to class table is stored in a dict.
"""

import threading
import time
import logging
import PyTango as tango
import numpy as np
from twisted.internet import reactor, defer, error
import TangoTwisted
import SpectrometerCameraController_twisted as SpectrometerCameraController
reload(TangoTwisted)
reload(SpectrometerCameraController)
from TangoTwisted import TangoAttributeFactory, defer_later

logger = logging.getLogger("SpectrometerCameraController")
logger.setLevel(logging.DEBUG)
while len(logger.handlers):
    logger.removeHandler(logger.handlers[0])

f = logging.Formatter("%(asctime)s - %(name)s.   %(funcName)s - %(levelname)s - %(message)s")
fh = logging.StreamHandler()
fh.setFormatter(f)
logger.addHandler(fh)
logger.setLevel(logging.DEBUG)


class StateDispatcher(object):
    def __init__(self, controller):
        self.controller = controller
        self.stop_flag = False
        self.statehandler_dict = dict()
        self.statehandler_dict[StateUnknown.name] = StateUnknown
        self.statehandler_dict[StateDeviceConnect.name] = StateDeviceConnect
        self.statehandler_dict[StateSetupAttributes.name] = StateSetupAttributes
        self.statehandler_dict[StateRunning.name] = StateRunning
        self.statehandler_dict[StateOn.name] = StateOn
        self.statehandler_dict[StateFault] = StateFault
        self.current_state = StateUnknown.name
        self._state_obj = None
        self._state_thread = None

        self.logger = logging.getLogger("SpectrometerCameraController.SpectrometerCameraStateStateDispatcher")
        self.logger.setLevel(logging.DEBUG)

    def statehandler_dispatcher(self):
        self.logger.info("Entering state handler dispatcher")
        prev_state = self.get_state()
        while self.stop_flag is False:
            # Determine which state object to construct:
            try:
                state_name = self.get_state_name()
                self.logger.debug("New state: {0}".format(state_name.upper()))
                self._state_obj = self.statehandler_dict[state_name](self.controller)
            except KeyError:
                state_name = "unknown"
                self.statehandler_dict[StateUnknown.name]
            self.controller.set_state(state_name)
            # Do the state sequence: enter - run - exit
            self._state_obj.state_enter(prev_state)
            self._state_obj.run()       # <- this should be run in a loop in state object and
            # return when it's time to change state
            new_state = self._state_obj.state_exit()
            # Set new state:
            self.set_state(new_state)
            prev_state = state_name
        self._state_thread = None

    def get_state(self):
        return self._state_obj

    def get_state_name(self):
        return self.current_state

    def set_state(self, state_name):
        try:
            self.logger.info("Current state: {0}, set new state {1}".format(self.current_state.upper(),
                                                                            state_name.upper()))
            self.current_state = state_name
        except AttributeError:
            logger.debug("New state unknown. Got {0}, setting to UNKNOWN".format(state_name))
            self.current_state = "unknown"

    def send_command(self, msg):
        self.logger.info("Sending command {0} to state {1}".format(msg, self.current_state))
        self._state_obj.check_message(msg)

    def stop(self):
        self.logger.info("Stop state handler thread")
        self._state_obj.stop_run()
        self.stop_flag = True

    def start(self):
        self.logger.info("Start state handler thread")
        if self._state_thread is not None:
            self.stop()
        self._state_thread = threading.Thread(target=self.statehandler_dispatcher)
        self._state_thread.start()


class State(object):
    name = ""

    def __init__(self, controller):
        self.controller = controller    # type: SpectrometerCameraController.SpectrometerCameraController
        self.logger = logging.getLogger("SpectrometerCameraController.{0}".format(self.name.upper()))
        # self.logger.name =
        self.logger.setLevel(logging.WARNING)
        self.deferred_list = list()
        self.next_state = None
        self.cond_obj = threading.Condition()
        self.running = False

    def state_enter(self, prev_state=None):
        self.logger.info("Entering state {0}".format(self.name.upper()))
        with self.cond_obj:
            self.running = True

    def state_exit(self):
        self.logger.info("Exiting state {0}".format(self.name.upper()))
        for d in self.deferred_list:
            try:
                d.cancel()
            except defer.CancelledError:
                pass
        return self.next_state

    def run(self):
        self.logger.info("Entering run, run condition {0}".format(self.running))
        with self.cond_obj:
            if self.running is True:
                self.cond_obj.wait()
        self.logger.debug("Exiting run")

    def check_requirements(self, result):
        """
        If next_state is None: stay on this state, else switch state
        :return:
        """
        self.next_state = None
        return result

    def check_message(self, msg):
        """
        Check message with condition object released and take appropriate action.
        The condition object is released already in the send_message function.

        -- This could be a message queue if needed...

        :param msg:
        :return:
        """
        pass

    def state_error(self, err):
        self.logger.error("Error {0} in state {1}".format(err, self.name.upper()))

    def get_name(self):
        return self.name

    def get_state(self):
        return self.name

    def send_message(self, msg):
        self.logger.info("Message {0} received".format(msg))
        with self.cond_obj:
            self.cond_obj.notify_all()
            self.check_message(msg)

    def stop_run(self):
        self.logger.info("Notify condition to stop run")
        with self.cond_obj:
            self.running = False
            self.logger.debug("Run condition {0}".format(self.running))
            self.cond_obj.notify_all()


class StateDeviceConnect(State):
    """
    Connect to tango devices needed.
    The names of the devices are stored in the controller.device_names list.
    Devices are stored as TangoAttributeFactories in controller.device_factory_dict

    """
    name = "device_connect"

    def __init__(self, controller):
        State.__init__(self, controller)
        self.controller.device_factory_dict = dict()
        self.deferred_list = list()

    def state_enter(self, prev_state):
        State.state_enter(self, prev_state)
        self.controller.set_status("Connecting to devices.")
        dl = list()
        for key, dev_name in self.controller.device_names.items():
            self.logger.debug("Connect to device {0}".format(dev_name))
            fact = TangoAttributeFactory(dev_name)
            dl.append(fact.startFactory())
            self.controller.device_factory_dict[dev_name] = fact
        self.logger.debug("List of deferred device proxys: {0}".format(dl))
        def_list = defer.DeferredList(dl)
        self.deferred_list.append(def_list)
        def_list.addCallbacks(self.check_requirements, self.state_error)

    def check_requirements(self, result):
        self.logger.info("Check requirements result: {0}".format(result))
        self.next_state = "setup_attributes"
        self.stop_run()
        return "setup_attributes"

    def state_error(self, err):
        self.logger.error("Error: {0}".format(err))
        self.controller.set_status("Error: {0}".format(err))
        # If the error was DB_DeviceNotDefined, go to UNKNOWN state and reconnect later
        self.next_state = "unknown"
        self.stop_run()


class StateSetupAttributes(State):
    """
    Setup attributes in the tango devices. Parameters stored in controller.setup_params
    Each key in setup_params is an attribute with the value as the value

    Device name is the name of the key in the controller.device_name dict (e.g. "camera").

    First the camera is put in ON state to be able to set certain attributes.
    When it is detected that the camera is in ON state the callback setup_attr is run,
    sending check_attributes on the attributes in the setup_params dict.

    """
    name = "setup_attributes"

    def __init__(self, controller):
        State.__init__(self, controller)
        self.deferred_list = list()

    def state_enter(self, prev_state=None):
        State.state_enter(self, prev_state)
        self.controller.set_status("Setting up device parameters.")
        self.logger.debug("Stopping camera before setting attributes")
        dl = list()
        d1 = self.controller.send_command("stop", "camera", None)
        d2 = self.controller.check_attribute("state", "camera", tango.DevState.ON, timeout=3.0, write=False)
        d2.addErrback(self.state_error)
        dl.append(d1)
        dl.append(d2)

        d = defer.DeferredList(dl)
        d.addCallbacks(self.setup_attr, self.state_error)
        self.deferred_list.append(d)

    def setup_attr(self, result):
        self.logger.info("Entering setup_attr")
        # Go through all the attributes in the setup_attr_params dict and add
        # do check_attribute with write to each.
        # The deferreds are collected in a list that is added to a DeferredList
        # When the DeferredList fires, the check_requirements method is called
        # as a callback.
        dl = list()
        for key in self.controller.setup_params:
            attr_name = key
            attr_value = self.controller.setup_params[key]
            dev_name = "camera"
            try:
                self.logger.debug("Setting attribute {0} on device {1} to {2}".format(attr_name.upper(),
                                                                                      dev_name.upper(),
                                                                                      attr_value))
            except AttributeError:
                self.logger.debug("Setting attribute according to: {0}".format(attr_name))
            if attr_value is not None:
                d = self.controller.check_attribute(attr_name, dev_name, attr_value, period=0.3, timeout=2.0,
                                                    write=True)
            else:
                d = self.controller.read_attribute(attr_name, dev_name)
            d.addCallbacks(self.attr_check_cb, self.attr_check_eb)
            dl.append(d)

        # Create DeferredList that will fire when all the attributes are done:
        def_list = defer.DeferredList(dl)
        self.deferred_list.append(def_list)
        def_list.addCallbacks(self.check_requirements, self.state_error)

    def check_requirements(self, result):
        self.logger.info("Check requirements")
        # self.logger.info("Check requirements result: {0}".format(result))
        self.next_state = "running"
        self.stop_run()
        return result

    def state_error(self, err):
        self.logger.error("Error: {0}".format(err))
        self.controller.set_status("Error: {0}".format(err))
        # If the error was DB_DeviceNotDefined, go to UNKNOWN state and reconnect later
        self.next_state = "unknown"
        self.stop_run()

    def attr_check_cb(self, result):
        self.logger.info("Check attribute result: {0}".format(result))
        self.controller.camera_result[result.name.lower()] = result
        return result

    def attr_check_eb(self, err):
        self.logger.error("Check attribute ERROR: {0}".format(error))
        return err


class StateRunning(State):
    """
    Wait for time for a new scan or a command. Parameters stored in controller.idle_params
    idle_params["scan_interval"]: time in seconds between scans
    """
    name = "running"

    def __init__(self, controller):
        State.__init__(self, controller)
        self.t0 = time.time()
        self.logger.setLevel(logging.WARNING)

    def state_enter(self, prev_state=None):
        State.state_enter(self, prev_state)
        # Start camera:
        self.controller.set_status("Starting spectrometer camera")
        d = self.controller.send_command("start", "camera", None)
        d.addCallbacks(self.check_requirements, self.state_error)
        self.deferred_list.append(d)
        # Start looping calls for monitored attributes
        dev_name = "camera"
        self.stop_looping_calls()
        for key in self.controller.running_attr_params:
            self.logger.debug("Starting looping call for {0}".format(key))
            interval = self.controller.running_attr_params[key]
            lc = TangoTwisted.LoopingCall(self.controller.read_attribute, key, dev_name)
            self.controller.looping_calls.append(lc)
            d = lc.start(interval)
            d.addCallbacks(self.update_attribute, self.state_error)
            lc.loop_deferred.addCallback(self.update_attribute)
            lc.loop_deferred.addErrback(self.state_error)

    def check_requirements(self, result):
        self.logger.info("Check requirements result: {0}".format(result))
        self.controller.set_status("Spectrometer running")
        return True

    def state_error(self, err):
        self.logger.error("Error: {0}".format(err))
        if err.type == defer.CancelledError:
            self.logger.info("Cancelled error, ignore")
        else:
            if self.running is True:
                self.controller.set_status("Error: {0}".format(err))
                self.stop_looping_calls()
                # If the error was DB_DeviceNotDefined, go to UNKNOWN state and reconnect later
                self.next_state = "unknown"
                self.stop_run()

    def check_message(self, msg):
        if msg == "stop":
            self.logger.debug("Message stop... set next state.")
            for d in self.deferred_list:
                d.cancel()
            self.stop_looping_calls()
            self.next_state = "on"
            self.stop_run()

    def stop_looping_calls(self):
        for lc in self.controller.looping_calls:
            # Stop looping calls (ignore callback):
            try:
                lc.stop()
            except Exception as e:
                self.logger.error("Could not stop looping call: {0}".format(e))
        self.controller.looping_calls = list()

    def update_attribute(self, result):
        self.logger.info("Updating result")
        try:
            self.logger.debug("Result for {0}: {1}".format(result.name, result.value))
        except AttributeError:
            return
        with self.controller.state_lock:
            self.controller.camera_result[result.name.lower()] = result
        if result.name.lower() == "image":
            self.calculate_spectrum(result)

    def calculate_spectrum(self, result):
        attr_image = result
        self.logger.debug("Calculating spectrum. Type image: {0}".format(type(attr_image)))
        with self.controller.state_lock:
            wavelengths = self.controller.camera_result["wavelengths"].value
            max_value = self.controller.camera_result["max_value"].value
        if attr_image is not None:
            quality = attr_image.quality
            a_time = attr_image.time
            spectrum = attr_image.value.sum(0)

            try:
                s_bkg = spectrum[0:10].mean()
                spec_bkg = spectrum - s_bkg
                l_peak = (spec_bkg * wavelengths).sum() / spec_bkg.sum()
                try:
                    dl_rms = np.sqrt((spec_bkg * (wavelengths - l_peak) ** 2).sum() / spec_bkg.sum())
                    dl_fwhm = dl_rms * 2 * np.sqrt(2 * np.log(2))
                except RuntimeWarning:
                    dl_rms = None
                    dl_fwhm = None
                nbr_sat = np.double((attr_image.value >= max_value).sum())
                sat_lvl = nbr_sat / np.size(attr_image.value)
            except AttributeError:
                l_peak = 0.0
                dl_fwhm = 0.0
                sat_lvl = 0.0
                quality = tango.AttrQuality.ATTR_INVALID
            except ValueError:
                # Dimension mismatch
                l_peak = 0.0
                dl_fwhm = 0.0
                sat_lvl = 0.0
                quality = tango.AttrQuality.ATTR_INVALID
            self.logger.debug("Spectrum parameters calculated")
            attr = tango.DeviceAttribute()
            attr.name = "spectrum"
            attr.value = spectrum
            attr.time = a_time
            attr.quality = quality
            self.controller.camera_result["spectrum"] = attr
            attr = tango.DeviceAttribute()
            attr.name = "width"
            attr.value = dl_fwhm
            attr.time = a_time
            attr.quality = quality
            self.controller.camera_result["width"] = attr
            attr = tango.DeviceAttribute()
            attr.name = "peak"
            attr.value = l_peak
            attr.time = a_time
            attr.quality = quality
            self.controller.camera_result["peak"] = attr
            attr = tango.DeviceAttribute()
            attr.name = "satlvl"
            attr.value = sat_lvl
            attr.time = a_time
            attr.quality = quality
            self.controller.camera_result["satlvl"] = attr


class StateOn(State):
    """
    Wait for time for a new scan or a command. Parameters stored in controller.idle_params
    idle_params["scan_interval"]: time in seconds between scans
    """
    name = "on"

    def __init__(self, controller):
        State.__init__(self, controller)
        self.t0 = time.time()

    def state_enter(self, prev_state=None):
        State.state_enter(self, prev_state)
        # d = defer.Deferred()
        self.controller.set_status("Stopping spectrometer camera")
        d = self.controller.send_command("stop", "camera", None)
        d.addCallbacks(self.check_requirements, self.state_error)
        self.deferred_list.append(d)

        dev_name = "camera"
        self.stop_looping_calls()
        for key in self.controller.standby_attr_params:
            self.logger.debug("Starting looping call for {0}".format(key))
            interval = self.controller.standby_attr_params[key]
            lc = TangoTwisted.LoopingCall(self.controller.read_attribute, key, dev_name)
            self.controller.looping_calls.append(lc)
            d = lc.start(interval)
            d.addCallbacks(self.update_attribute, self.state_error)
            lc.loop_deferred.addCallback(self.update_attribute)
            lc.loop_deferred.addErrback(self.state_error)

    def check_requirements(self, result):
        self.logger.info("Check requirements result: {0}".format(result))
        self.controller.set_status("Spectrometer standby")
        return True

    def state_error(self, err):
        self.logger.error("Error: {0}".format(err))
        if err.type == defer.CancelledError:
            self.logger.info("Cancelled error, ignore")
        else:
            self.controller.set_status("Error: {0}".format(err))
            self.stop_looping_calls()
            # If the error was DB_DeviceNotDefined, go to UNKNOWN state and reconnect later
            self.next_state = "unknown"
            self.stop_run()

    def stop_looping_calls(self):
        for lc in self.controller.looping_calls:
            # Stop looping calls (ignore callback):
            try:
                lc.stop()
            except Exception as e:
                self.logger.error("Could not stop looping call: {0}".format(e))
        self.controller.looping_calls = list()

    def check_message(self, msg):
        if msg == "start":
            self.logger.debug("Message start... set next state.")
            for d in self.deferred_list:
                d.cancel()
            self.stop_looping_calls()

            self.next_state = "running"
            self.stop_run()

    def update_attribute(self, result):
        self.logger.info("Updating result")
        try:
            self.logger.debug("Result for {0}: {1}".format(result.name, result.value))
        except AttributeError:
            return
        with self.controller.state_lock:
            self.controller.camera_result[result.name.lower()] = result


class StateFault(State):
    """
    Handle fault condition.
    """
    name = "fault"

    def __init__(self, controller):
        State.__init__(self, controller)


class StateUnknown(State):
    """
    Limbo state.
    Wait and try to connect to devices.
    """
    name = "unknown"

    def __init__(self, controller):
        State.__init__(self, controller)
        self.deferred_list = list()
        self.start_time = None
        self.wait_time = 1.0

    def state_enter(self, prev_state):
        self.logger.info("Starting state {0}".format(self.name.upper()))
        self.controller.set_status("Waiting {0} s before trying to reconnect".format(self.wait_time))
        self.start_time = time.time()
        df = defer_later(self.wait_time, self.check_requirements, [None])
        self.deferred_list.append(df)
        df.addCallback(test_cb)
        self.running = True

    def check_requirements(self, result):
        self.logger.info("Check requirements result {0} for state {1}".format(result, self.name.upper()))
        self.next_state = "device_connect"
        self.stop_run()


def test_cb(result):
    logger.debug("Returned {0}".format(result))


def test_err(err):
    logger.error("ERROR Returned {0}".format(err))


if __name__ == "__main__":
    fc = SpectrometerCameraController.SpectrometerCameraController("gunlaser/cameras/spectrometer_camera")

    sh = StateDispatcher(fc)
    sh.start()
