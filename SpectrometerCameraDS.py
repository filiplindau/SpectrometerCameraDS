# -*- coding:utf-8 -*-
"""
Created on Jan 23, 2018

@author: Filip Lindau
"""
import threading
import time
import PyTango as pt
from PyTango.server import Device, DeviceMeta
from PyTango.server import attribute
from PyTango.server import device_property
from CameraDeviceController_2 import CameraDeviceController
import numpy as np


class SpectrometerCameraDS(Device):
    __metaclass__ = DeviceMeta

    exposuretime = attribute(label='ExposureTime',
                             dtype=float,
                             access=pt.AttrWriteType.READ_WRITE,
                             unit="us",
                             format="%8.1f",
                             min_value=0.0,
                             max_value=1e7,
                             fget="get_exposuretime",
                             fset="set_exposuretime",
                             doc="Camera exposure time in us",
                             memorized=True,)
                             # hw_memorized=True)

    gain = attribute(label='Gain',
                     dtype=float,
                     access=pt.AttrWriteType.READ_WRITE,
                     unit="dB",
                     format="%3.2f",
                     min_value=0.0,
                     max_value=1e2,
                     fget="get_gain",
                     fset="set_gain",
                     doc="Camera gain in dB",
                     memorized=True,)
                     # hw_memorized=True)

    wavelengthvector = attribute(label='WavelengthVector',
                                 dtype=[np.double],
                                 access=pt.AttrWriteType.READ,
                                 max_dim_x=16384,
                                 display_level=pt.DispLevel.OPERATOR,
                                 unit="nm",
                                 format="%5.2f",
                                 fget="get_wavelengthvector",
                                 doc="Wavelength vector",
                                 )

    spectrum = attribute(label='Spectrum',
                         dtype=[np.double],
                         access=pt.AttrWriteType.READ,
                         max_dim_x=16384,
                         display_level=pt.DispLevel.OPERATOR,
                         unit="a.u.",
                         format="%5.2f",
                         fget="get_spectrum",
                         doc="Spectrum",
                         )

    camera_name = device_property(dtype=str,
                                  doc="Tango name of the camera device",
                                  default_value="gunlaser/cameras/spectrometer_camera")

    watchdog_timeout = device_property(dtype=float,
                                       doc="Timeout for the watchdog resetting the hardware in s",
                                       default_value="2.0")

    dispersion = device_property(dtype=float,
                                 doc="Dispersion of the spectrometer in nm/px. "
                                     "Positive if wavelength increases to the right",
                                 default_value="0.056")

    central_wavelength = device_property(dtype=float,
                                         doc="Wavelength of the central pixel of the ROI in nm",
                                         default_value="2.0")

    roi = device_property(dtype=[int],
                          doc="Wavelength of the central pixel of the ROI in nm",
                          default_value=[0, 0, 100, 100])

    def __init__(self, klass, name):
        self.wavelengthvector_data = np.array([])
        self.dev_controller = None
        self.db = None
        Device.__init__(self, klass, name)

    def init_device(self):
        self.debug_stream("In init_device:")
        Device.init_device(self)
        self.db = pt.Database()
        self.set_state(pt.DevState.UNKNOWN)
        self.debug_stream("Init camera controller {0}".format(self.camera_name))
        params = dict()
        params["imageoffsetx"] = self.roi[0]
        params["imageoffsety"] = self.roi[1]
        params["imagewidth"] = self.roi[2]
        params["imageheight"] = self.roi[3]
        params["triggermode"] = "Off"
        try:
            if self.dev_controller is not None:
                self.dev_controller.stop_thread()
        except Exception as e:
            self.error_info("Error stopping camera controller: {0}".format(e))
        try:
            self.dev_controller = CameraDeviceController(self.camera_name, params)
            self.setup_camera()
        except Exception as e:
            self.error_stream("Error creating camera controller: {0}".format(e))
            return

        self.debug_stream("init_device finished")
        # self.set_state(pt.DevState.ON)
        self.dev_controller.add_state_callback(self.change_state)

    def setup_camera(self):
        self.info_stream("Entering setup_camera")
        self.wavelengthvector_data = (self.central_wavelength + np.arange(-self.roi[2] / 2,
                                                                          self.roi[2] / 2) * self.dispersion) * 1e-9

        # cmd0 = self.dev_controller.exec_command("stop")
        # cmd_d = self.dev_controller.delay_command(1.0, after_cmd=cmd0)
        # cmd1 = self.dev_controller.write_attribute("imageoffsetx", self.roi[0], after_cmd=cmd_d)
        # cmd2 = self.dev_controller.write_attribute("imageoffsety", self.roi[1], after_cmd=cmd_d)
        # cmd3 = self.dev_controller.write_attribute("imagewidth", self.roi[2], after_cmd=cmd_d)
        # cmd4 = self.dev_controller.write_attribute("imageheight", self.roi[3], after_cmd=cmd_d)
        # self.dev_controller.exec_command("start", after_cmd=[cmd1, cmd2, cmd3, cmd4])

    def change_state(self, new_state, new_status=None):
        self.debug_stream("Change state from {0} to {1}".format(self.get_state(), new_state))
        if self.get_state() is pt.DevState.INIT and new_state is not pt.DevState.UNKNOWN:
            self.debug_stream("Set memorized attributes")
            data = self.db.get_device_attribute_property(self.get_name(), "gain")
            self.debug_stream("Database returned data for \"gain\": {0}".format(data["gain"]))
            try:
                new_value = float(data["gain"]["__value"][0])
                self.debug_stream("{0}".format(new_value))
                self.dev_controller.write_attribute("gain", new_value)
            except (KeyError, TypeError, IndexError, ValueError):
                pass
            data = self.db.get_device_attribute_property(self.get_name(), "exposuretime")
            self.debug_stream("Database returned data for \"exposuretime\": {0}".format(data["exposuretime"]))
            try:
                new_value = float(data["exposuretime"]["__value"][0])
                self.dev_controller.write_attribute("exposuretime", new_value)
            except (KeyError, TypeError, IndexError, ValueError):
                pass
        self.set_state(new_state)
        if new_status is not None:
            self.debug_stream("Setting status {0}".format(new_status))
            self.set_status(new_status)

    def get_spectrum(self):
        attr = self.dev_controller.get_attribute("image")
        try:
            spectrum = attr.value.sum(0)
        except AttributeError:
            spectrum = []
        return spectrum, attr.time.totime(), attr.quality

    def get_wavelengthvector(self):
        self.debug_stream("get_wavelengthvector: size {0}".format(self.wavelengthvector_data.shape))
        return self.wavelengthvector_data, time.time(), pt.AttrQuality.ATTR_VALID

    def get_exposuretime(self):
        attr = self.dev_controller.get_attribute("exposuretime")
        return attr.value, attr.time.totime(), attr.quality

    def set_exposuretime(self, new_exposuretime):
        self.debug_stream("In set_exposuretime: New value {0}".format(new_exposuretime))
        self.debug_stream("Type dev_controller: {0}".format(type(self.dev_controller)))
        self.dev_controller.write_attribute("exposuretime", new_exposuretime)

    def get_gain(self):
        attr = self.dev_controller.get_attribute("gain")
        return attr.value, attr.time.totime(), attr.quality

    def set_gain(self, new_gain):
        self.debug_stream("In set_gain: New value {0}".format(new_gain))
        self.dev_controller.write_attribute("gain", new_gain)


if __name__ == "__main__":
    pt.server.server_run((SpectrometerCameraDS, ))
