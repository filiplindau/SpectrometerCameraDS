# -*- coding:utf-8 -*-
"""
Created on Apr 18, 2018

@author: Filip Lindau
"""
import threading
import time
import PyTango as tango
from PyTango.server import Device, DeviceMeta
from PyTango.server import attribute, command
from PyTango.server import device_property
from SpectrometerCameraController_twisted import SpectrometerCameraController
from SpectrometerCameraState import StateDispatcher
from CameraDeviceController_2 import CameraDeviceController
import numpy as np
import logging


logger = logging.getLogger("SpectrometerCameraController")
logger.setLevel(logging.DEBUG)
while len(logger.handlers):
    logger.removeHandler(logger.handlers[0])

f = logging.Formatter("%(asctime)s - %(name)s.   %(funcName)s - %(levelname)s - %(message)s")
fh = logging.StreamHandler()
fh.setFormatter(f)
logger.addHandler(fh)
logger.setLevel(logging.DEBUG)


class SpectrometerCameraDS(Device):
    __metaclass__ = DeviceMeta

    exposuretime = attribute(label='ExposureTime',
                             dtype=float,
                             access=tango.AttrWriteType.READ_WRITE,
                             unit="us",
                             format="%8.1f",
                             min_value=0.0,
                             max_value=1e7,
                             fget="get_exposuretime",
                             fset="set_exposuretime",
                             doc="Camera exposure time in us",
                             memorized=True, )

    gain = attribute(label='Gain',
                     dtype=float,
                     access=tango.AttrWriteType.READ_WRITE,
                     unit="dB",
                     format="%3.2f",
                     min_value=0.0,
                     max_value=1e2,
                     fget="get_gain",
                     fset="set_gain",
                     doc="Camera gain in dB",
                     memorized=True, )

    wavelengthvector = attribute(label='WavelengthVector',
                                 dtype=[np.double],
                                 access=tango.AttrWriteType.READ,
                                 max_dim_x=16384,
                                 display_level=tango.DispLevel.OPERATOR,
                                 unit="m",
                                 format="%5.2e",
                                 fget="get_wavelengthvector",
                                 doc="Wavelength vector",
                                 )

    spectrum = attribute(label='Spectrum',
                         dtype=[np.double],
                         access=tango.AttrWriteType.READ,
                         max_dim_x=16384,
                         display_level=tango.DispLevel.OPERATOR,
                         unit="a.u.",
                         format="%5.2f",
                         fget="get_spectrum",
                         doc="Spectrum",
                         )

    width = attribute(label='Spectrum width FWHM',
                      dtype=np.double,
                      access=tango.AttrWriteType.READ,
                      display_level=tango.DispLevel.OPERATOR,
                      unit="m",
                      format="%5.2e",
                      fget="get_width",
                      doc="FWHM for the peak in spectrum. Basic thresholding and peak detection is used.",
                      )

    peak = attribute(label='Spectrum peak',
                     dtype=np.double,
                     access=tango.AttrWriteType.READ,
                     display_level=tango.DispLevel.OPERATOR,
                     unit="m",
                     format="%5.2e",
                     fget="get_peak",
                     doc="Wavelength for the peak in spectrum. Basic thresholding and peak detection is used.",
                     )

    sat_lvl = attribute(label='Saturation level',
                        dtype=np.double,
                        access=tango.AttrWriteType.READ,
                        display_level=tango.DispLevel.OPERATOR,
                        unit="relative",
                        format="%2.2e",
                        fget="get_satlvl",
                        doc="Relative amount of pixels that are saturated. This should be zero.",
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
                          doc="Pixel coordinates for the ROI: [left, top, width, height]",
                          default_value=[0, 0, 100, 100])

    saturation_level = device_property(dtype=int,
                          doc="Saturation pixel value, used for estimating overexposure",
                          default_value=65536)

    def __init__(self, klass, name):
        self.wavelengthvector_data = np.array([])
        self.max_value = 1.0
        self.controller = None              # type: SpectrometerCameraController
        self.state_dispatcher = None        # type: StateDispatcher
        self.db = None
        Device.__init__(self, klass, name)

    def init_device(self):
        self.debug_stream("In init_device:")
        Device.init_device(self)
        self.db = tango.Database()
        self.set_state(tango.DevState.UNKNOWN)
        try:
            if self.state_dispatcher is not None:
                self.state_dispatcher.stop()
        except Exception as e:
            self.error_info("Error stopping state dispatcher: {0}".format(e))
        try:
            self.controller = SpectrometerCameraController(self.camera_name)
            self.controller.add_state_notifier(self.change_state)
        except Exception as e:
            self.error_stream("Error creating camera controller: {0}".format(e))
            return
        self.setup_params()
        self.setup_spectrometer()

        self.state_dispatcher = StateDispatcher(self.controller)
        self.state_dispatcher.start()

        self.debug_stream("init_device finished")

    def setup_params(self):
        params = dict()
        params["imageoffsetx"] = self.roi[0]
        params["imageoffsety"] = self.roi[1]
        params["imagewidth"] = self.roi[2]
        params["imageheight"] = self.roi[3]
        params["triggermode"] = "Off"
        self.controller.setup_params = params

    def setup_spectrometer(self):
        self.info_stream("Entering setup_camera")
        self.wavelengthvector_data = (self.central_wavelength + np.arange(-self.roi[2] / 2,
                                                                          self.roi[2] / 2) * self.dispersion) * 1e-9
        self.max_value = self.saturation_level
        wavelength_attr = tango.DeviceAttribute()
        wavelength_attr.name = "wavelengths"
        wavelength_attr.quality = tango.AttrQuality.ATTR_VALID
        wavelength_attr.value = self.wavelengthvector_data
        # wavelength_attr.data_format = tango.AttrDataFormat.SPECTRUM
        wavelength_attr.time = tango.time_val.TimeVal(time.time())
        max_value_attr = tango.DeviceAttribute()
        max_value_attr.name = "max_value"
        max_value_attr.quality = tango.AttrQuality.ATTR_VALID
        max_value_attr.value = self.max_value
        # max_value_attr.data_format = tango.AttrDataFormat.SCALAR
        max_value_attr.time = tango.time_val.TimeVal(time.time())
        with self.controller.state_lock:
            self.controller.camera_result["wavelengths"] = wavelength_attr
            self.controller.camera_result["max_value"] = max_value_attr

    def change_state(self, new_state, new_status=None):
        self.info_stream("Change state: {0}, status {1}".format(new_state, new_status))
        # Map new_state string to tango state
        if new_state in ["running"]:
            tango_state = tango.DevState.RUNNING
        elif new_state in ["on"]:
            tango_state = tango.DevState.ON
        elif new_state in ["device_connect", "setup_attributes"]:
            tango_state = tango.DevState.INIT
        elif new_state in ["fault"]:
            tango_state = tango.DevState.FAULT
        else:
            tango_state = tango.DevState.UNKNOWN

        # Set memorized attributes when entering init from unknown state:
        if self.get_state() is tango.DevState.INIT and new_state is not tango.DevState.UNKNOWN:
            self.debug_stream("Set memorized attributes")
            try:
                data = self.db.get_device_attribute_property(self.get_name(), "gain")
                self.debug_stream("Database returned data for \"gain\": {0}".format(data["gain"]))
            except TypeError as e:
                self.warn_stream("Gain not found in database. {0}".format(e))
            try:
                new_value = float(data["gain"]["__value"][0])
                self.debug_stream("{0}".format(new_value))
                self.controller.write_attribute("gain", "camera", new_value)
            except (KeyError, TypeError, IndexError, ValueError):
                pass
            try:
                data = self.db.get_device_attribute_property(self.get_name(), "exposuretime")
                self.debug_stream("Database returned data for \"exposuretime\": {0}".format(data["exposuretime"]))
            except TypeError as e:
                self.warn_stream("Exposuretime not found in database. {0}".format(e))
            try:
                new_value = float(data["exposuretime"]["__value"][0])
                self.controller.write_attribute("exposuretime", "camera", new_value)
            except (KeyError, TypeError, IndexError, ValueError):
                pass

        if tango_state != self.get_state():
            self.debug_stream("Change state from {0} to {1}".format(self.get_state(), new_state))
            self.set_state(tango_state)
        if new_status is not None:
            self.debug_stream("Setting status {0}".format(new_status))
            self.set_status(new_status)

    def get_spectrum(self):
        attr = self.controller.get_attribute("spectrum")
        try:
            self.debug_stream("get_spectrum: {0}".format(attr.value.shape))
        except Exception as e:
            self.warn_stream("Could not format attribute: {0}".format(e))
        return attr.value, attr.time.totime(), attr.quality

    def get_wavelengthvector(self):
        self.debug_stream("get_wavelengthvector: size {0}".format(self.wavelengthvector_data.shape))
        attr = self.controller.get_attribute("wavelengths")
        return attr.value, attr.time.totime(), attr.quality

    def get_exposuretime(self):
        attr = self.controller.get_attribute("exposuretime")
        try:
            self.debug_stream("get_exposuretime: {0}".format(attr))
        except Exception as e:
            self.warn_stream("Could not format attribute: {0}".format(e))
        return attr.value, attr.time.totime(), attr.quality

    def set_exposuretime(self, new_exposuretime):
        self.debug_stream("In set_exposuretime: New value {0}".format(new_exposuretime))
        # self.controller.write_attribute("exposuretime", "camera", new_exposuretime)
        try:
            old_exposure = self.controller.get_attribute("exposuretime").value
        except AttributeError:
            old_exposure = 0.0
        tol = np.abs(new_exposuretime - old_exposure) * 0.1
        self.controller.check_attribute("exposuretime", "camera", new_exposuretime,
                                        tolerance=tol, write=True)

    def get_gain(self):
        attr = self.controller.get_attribute("gain")
        return attr.value, attr.time.totime(), attr.quality

    def set_gain(self, new_gain):
        self.debug_stream("In set_gain: New value {0}".format(new_gain))
        self.controller.write_attribute("gain", "camera", new_gain)
        self.controller.read_attribute("gain", "camera")

    def get_width(self):
        attr = self.controller.get_attribute("width")
        return attr.value, attr.time.totime(), attr.quality

    def get_peak(self):
        attr = self.controller.get_attribute("peak")
        return attr.value, attr.time.totime(), attr.quality

    def get_satlvl(self):
        attr = self.controller.get_attribute("satlvl")
        return attr.value, attr.time.totime(), attr.quality

    @command(doc_in="Start spectrometer.")
    def start(self):
        """Start spectrometer camera"""
        self.info_stream("Starting spectrometer")
        self.state_dispatcher.send_command("start")

    @command(doc_in="Stop spectrometer.")
    def stop(self):
        """Stop spectrometer camera"""
        self.info_stream("Stopping spectrometer")
        self.state_dispatcher.send_command("stop")


if __name__ == "__main__":
    tango.server.server_run((SpectrometerCameraDS,))
