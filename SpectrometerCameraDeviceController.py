# -*- coding:utf-8 -*-
"""
Created on Feb 14, 2018

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
from CameraDeviceController_2 import CameraDeviceController

root = logging.getLogger("CameraDeviceController")
while len(root.handlers):
    root.removeHandler(root.handlers[0])

f = logging.Formatter("%(asctime)s - %(module)s.   %(funcName)s - %(levelname)s - %(message)s")
fh = logging.StreamHandler()
fh.setFormatter(f)
root.addHandler(fh)
root.setLevel(logging.DEBUG)


class SpectrometerCameraDeviceController(CameraDeviceController):
    def __init__(self, device_name, parameter_dict=None, wavelength_vector=None, max_value=65536):
        CameraDeviceController.__init__(self, device_name, parameter_dict)
        self.spectrum = None
        self.spectrum_width = None
        self.spectrum_peak = None
        self.wavelengths = wavelength_vector
        self.max_value = max_value

    def device_command_cb(self, cmd_d):
        calc_spectrum = False
        if cmd_d.name == "image":
            if cmd_d.done is True:
                calc_spectrum = True
                root.debug("Calculate spectrum")
        CameraDeviceController.device_command_cb(self, cmd_d)
        if calc_spectrum is True:
            self.calculate_spectrum()

    def calculate_spectrum(self):
        with self.lock:
            attr_image = self.attribute_dict["image"]
            root.debug("Calculating spectrum. Type image: {0}".format(type(attr_image)))
            if attr_image is not None:
                quality = attr_image.quality
                a_time = attr_image.time.totime()
                self.spectrum = attr_image.value.sum(0)
                self.attribute_dict["spectrum"] = (self.spectrum, a_time, quality)

                try:
                    s_bkg = self.spectrum[0:10].mean()
                    spec_bkg = self.spectrum - s_bkg
                    l_peak = (spec_bkg * self.wavelengths).sum() / spec_bkg.sum()
                    dl_rms = np.sqrt((spec_bkg * (self.wavelengths - l_peak) ** 2).sum() / spec_bkg.sum())
                    dl_fwhm = dl_rms * 2 * np.sqrt(2 * np.log(2))
                    nbr_sat = np.double((attr_image.value >= self.max_value).sum())
                    sat_lvl = nbr_sat / np.size(attr_image.value)
                except AttributeError:
                    l_peak = 0.0
                    dl_fwhm = 0.0
                    sat_lvl = 0.0
                    quality = pt.AttrQuality.ATTR_INVALID
                except ValueError:
                    # Dimension mismatch
                    l_peak = 0.0
                    dl_fwhm = 0.0
                    sat_lvl = 0.0
                    quality = pt.AttrQuality.ATTR_INVALID
                self.attribute_dict["width"] = (dl_fwhm, a_time, quality)
                self.attribute_dict["peak"] = (l_peak, a_time, quality)
                self.attribute_dict["satlvl"] = (sat_lvl, a_time, quality)
