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
from CameraDeviceController_futures import CameraDeviceController
import numpy as np


class SpectrometerCameraDS(Device, CameraDeviceController):
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
                             memorized=True,
                             hw_memorized=True)

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
                     memorized=True,
                     hw_memorized=True)

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
        Device.__init__(self, klass, name)
        self.wavelengthvector_data = np.array([])

    def init_device(self):
        self.debug_stream("In init_device:")
        Device.init_device(self)
        self.debug_stream("Init camera controller {0}".format(self.camera_name))
        CameraDeviceController.__init__(self, self.camera_name)
        self.setup_camera()

    def setup_camera(self):
        self.info_stream("Entering setup_camera")
        self.wavelengthvector_data = self.central_wavelength + np.arange(-self.roi[2] / 2,
                                                                         self.roi[2] / 2) * self.dispersion

        self.write_attribute("imageoffsetx", self.roi[0])
        self.write_attribute("imageoffsety", self.roi[1])
        self.write_attribute("imagewidth", self.roi[2])
        self.write_attribute("imageheight", self.roi[3])

    def get_spectrum(self):
        attr = self.get_attribute("image")
        try:
            spectrum = attr.value.sum(1)
        except AttributeError:
            spectrum = []
        return spectrum, attr.time.totime(), attr.quality

    def get_wavelengthvector(self):
        self.debug_stream("get_wavelengthvector: size {0}".format(self.wavelengthvector_data.shape))
        return self.wavelengthvector_data, time.time(), pt.AttrQuality.ATTR_VALID

    def get_exposuretime(self):
        attr = self.get_attribute("exposuretime")
        return attr.value, attr.time.totime(), attr.quality

    def set_exposuretime(self, new_exposuretime):
        self.debug_stream("In set_exposuretime: New value {0}".format(new_exposuretime))
        self.write_attribute("exposuretime", new_exposuretime)

    def get_gain(self):
        attr = self.get_attribute("gain")
        return attr.value, attr.time.totime(), attr.quality

    def set_gain(self, new_gain):
        self.debug_stream("In set_gain: New value {0}".format(new_gain))
        self.write_attribute("gain", new_gain)


if __name__ == "__main__":
    pt.server.server_run((SpectrometerCameraDS, ))
