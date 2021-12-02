# This file is part of the pyBinSim project.
#
# Copyright (c) 2017 A. Neidhardt, F. Klein, N. Knoop, T. Köllmer
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

""" Module contains main loop and configuration of pyBinSim """
import logging
import time
import sys

import numpy as np
import sounddevice as sd

from pybinsim.convolver import ConvolverFFTW
from pybinsim.filterstorage import FilterStorage
from pybinsim.osc_receiver import OscReceiver
from pybinsim.pose import Pose
from pybinsim.soundhandler import SoundHandler
from pybinsim.input_buffer import InputBuffer

import timeit
import pydevd

def parse_boolean(any_value):

    if type(any_value) == bool:
        return any_value

    # str -> bool
    if any_value == 'True':
        return True
    if any_value == 'False':
        return False

    return None


class BinSimConfig(object):
    def __init__(self):

        self.log = logging.getLogger("pybinsim.BinSimConfig")

        # Default Configuration
        self.configurationDict = {'soundfile': '',
                                  'blockSize': 256,
                                  'ds_filterSize': 512,
                                  'early_filterSize': 4096,
                                  'late_filterSize': 16384,
                                  'filterList': 'brirs/filter_list_kemar5.txt',
                                  'enableCrossfading': False,
                                  'useHeadphoneFilter': False,
                                  'headphone_filterSize': 4096,
                                  'loudnessFactor': float(1),
                                  'maxChannels': 8,
                                  'samplingRate': 48000,
                                  'loopSound': True,
                                  'pauseConvolution': False,
                                  'pauseAudioPlayback': False}

    def read_from_file(self, filepath):
        config = open(filepath, 'r')

        for line in config:
            line_content = str.split(line)
            key = line_content[0]
            value = line_content[1]

            if key in self.configurationDict:
                config_value_type = type(self.configurationDict[key])

                if config_value_type is bool:
                    # evaluate 'False' to False
                    boolean_config = parse_boolean(value)

                    if boolean_config is None:
                        self.log.warning(
                            "Cannot convert {} to bool. (key: {}".format(value, key))

                    self.configurationDict[key] = boolean_config
                else:
                    # use type(str) - ctors of int, float, ...
                    self.configurationDict[key] = config_value_type(value)

            else:
                self.log.warning('Entry ' + key + ' is unknown')

    def get(self, setting):
        return self.configurationDict[setting]

    def set(self, setting, value):
        value = parse_boolean(value)
        if type(self.configurationDict[setting]) == type(value):
            self.configurationDict[setting] = value
        else:
            self.log.warning('New value for entry ' + setting + ' has wrong type: ' + str(type(value)))

class BinSim(object):
    """
    Main pyBinSim program logic
    """

    def __init__(self, config_file):

        self.log = logging.getLogger("pybinsim.BinSim")
        self.log.info("BinSim: init")

        self.cb_time_usage = np.array(range(0, 50), dtype='float32')
        self.cpu_usage_update_rate = 100

        # Read Configuration File
        self.config = BinSimConfig()
        self.config.read_from_file(config_file)

        self.nChannels = self.config.get('maxChannels')
        self.sampleRate = self.config.get('samplingRate')
        self.blockSize = self.config.get('blockSize')

        self.result = None
        self.block = None
        self.stream = None

        self.convolverWorkers = []
        self.convolverHP, self.ds_convolvers, self.early_convolvers, self.late_convolvers, self.input_BufferMono,\
        self.input_BufferStereo, self.filterStorage,  self.oscReceiver, self.soundHandler = self.initialize_pybinsim()


    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.__cleanup()

    def stream_start(self):
        self.log.info("BinSim: stream_start")
        try:
            self.stream = sd.OutputStream(samplerate=self.sampleRate,
                                          dtype=np.float32,
                                          channels=2,
                                          latency="low",
                                          blocksize=self.blockSize,
                                          callback=audio_callback(self))

           #pydevd.settrace(suspend=False, trace_only_current_thread=True)

            with self.stream as s:
                self.log.info(f"latency: {s.latency} seconds")
                while True:
                    sd.sleep(1000)


        except KeyboardInterrupt:
            print("KEYBOARD")
        except Exception as e:
            print(e)

    def initialize_pybinsim(self):
        self.result = np.empty([self.blockSize, 2], dtype=np.float32)
        self.block = np.empty(
            [self.nChannels, self.blockSize], dtype=np.float32)

        ds_size = self.config.get('ds_filterSize')
        early_size = self.config.get('early_filterSize')
        late_size = self.config.get('late_filterSize')

        if ds_size < self.blockSize:
            ds_size = self.blockSize
            self.log.info('Block size smaller than direct sound filter size: Zero Padding DS filter')
        if early_size < self.blockSize:
            early_size = self.blockSize
            self.log.info('Block size smaller than early filter size: Zero Padding EARLY filter')
        if late_size < self.blockSize:
            late_size = self.blockSize
            self.log.info('Block size smaller than late filter size: Zero Padding LATE filter')

        # Create FilterStorage
        filterStorage = FilterStorage(self.blockSize,
                                      self.config.get('filterList'),
                                      self.config.get('useHeadphoneFilter'),
                                      self.config.get('headphone_filterSize'),
                                      ds_size,
                                      early_size,
                                      late_size )

        # Start an oscReceiver
        oscReceiver = OscReceiver(self.config)
        oscReceiver.start_listening()
        time.sleep(1)

        # Create SoundHandler
        soundHandler = SoundHandler(self.blockSize, self.nChannels,
                                    self.sampleRate, self.config.get('loopSound'))

        soundfile_list = self.config.get('soundfile')
        soundHandler.request_new_sound_file(soundfile_list)

        # Create input buffers
        input_BufferMono = InputBuffer(self.blockSize,  False)
        input_BufferStereo = InputBuffer(self.blockSize,  True)


        # Create N convolvers depending on the number of wav channels
        self.log.info('Number of Channels: ' + str(self.nChannels))
        ds_convolvers = [None] * self.nChannels
        early_convolvers = [None] * self.nChannels
        late_convolvers = [None] * self.nChannels

        for n in range(self.nChannels):
            ds_convolvers[n] = ConvolverFFTW(ds_size, self.blockSize, False)
            early_convolvers[n] = ConvolverFFTW(early_size, self.blockSize, False)
            late_convolvers[n] = ConvolverFFTW(late_size, self.blockSize, False)

        # HP Equalization convolver
        convolverHP = None
        if self.config.get('useHeadphoneFilter'):
            convolverHP = ConvolverFFTW(self.config.get(
                'headphone_filterSize'), self.blockSize, True)
            hpfilter = filterStorage.get_headphone_filter()
            convolverHP.setIR(hpfilter, False)

        return convolverHP, ds_convolvers, early_convolvers, late_convolvers, input_BufferMono, input_BufferStereo, \
               filterStorage, oscReceiver, soundHandler

    def __cleanup(self):
        # Close everything when BinSim is finished
        self.oscReceiver.close()
        self.stream.close()
        self.filterStorage.close()

        for n in range(self.nChannels):
            self.ds_convolvers[n].close()
            self.early_convolvers[n].close()
            self.late_convolvers[n].close()

        if self.config.get('useHeadphoneFilter'):
            if self.convolverHP:
                self.convolverHP.close()


def audio_callback(binsim):
    """ Wrapper for callback to hand over custom data """
    assert isinstance(binsim, BinSim)

    # The python-sounddevice Callback
    def callback(outdata, frame_count, time_info, status):
        # print("python-sounddevice callback")

        cb_start = timeit.default_timer()
        count = 0

        if "debugpy" in sys.modules:
            import debugpy
            debugpy.debug_this_thread()

        # Update config
        binsim.current_config = binsim.oscReceiver.get_current_config()

        # Update audio files
        current_soundfile_list = binsim.oscReceiver.get_sound_file_list()
        if current_soundfile_list:
            binsim.soundHandler.request_new_sound_file(current_soundfile_list)

        # Get sound block. At least one convolver should exist
        amount_channels = binsim.soundHandler.get_sound_channels()
        if amount_channels == 0:
            return

        if binsim.current_config.get('pauseAudioPlayback'):
            binsim.block[:amount_channels, :] = binsim.soundHandler.read_zeros()
        else:
            binsim.block[:amount_channels, :] = binsim.soundHandler.buffer_read()

        if binsim.current_config.get('pauseConvolution'):
            if binsim.soundHandler.get_sound_channels() == 2:
                binsim.result = np.transpose(binsim.block[:binsim.soundHandler.get_sound_channels(), :])
            else:
                mix = np.mean(binsim.block[:binsim.soundHandler.get_sound_channels(), :], 0)
                binsim.result[:, 0] = mix
                binsim.result[:, 1] = mix
        else:
            # Update Filters and run each convolver with the current block
            for n in range(amount_channels):

                # Get new Filter
                if binsim.oscReceiver.is_ds_filter_update_necessary(n):
                    ds_filterValueList = binsim.oscReceiver.get_current_ds_filter_values(n)
                    ds_filter = binsim.filterStorage.get_ds_filter(Pose.from_filterValueList(ds_filterValueList))
                    binsim.ds_convolvers[n].setIR(ds_filter, callback.config.get('enableCrossfading'))

                # Get new early reverb Filter
                if binsim.oscReceiver.is_early_filter_update_necessary(n):
                    early_filterValueList = binsim.oscReceiver.get_current_early_filter_values(n)
                    early_filter = binsim.filterStorage.get_early_filter(Pose.from_filterValueList(early_filterValueList))
                    binsim.early_convolvers[n].setIR(early_filter, callback.config.get('enableCrossfading'))

                # Get new late reverb Filter
                if binsim.oscReceiver.is_late_filter_update_necessary(n):
                    late_filterValueList = binsim.oscReceiver.get_current_late_filter_values(n)
                    late_filter = binsim.filterStorage.get_late_filter(Pose.from_filterValueList(late_filterValueList))
                    binsim.late_convolvers[n].setIR(late_filter, callback.config.get('enableCrossfading'))

                input_mono = binsim.input_BufferMono.process(binsim.block[n, :])

                left_ds, right_ds, count = binsim.ds_convolvers[n].process(input_mono)
                left_early, right_early, _ = binsim.early_convolvers[n].process(input_mono)
                left_late, right_late, _ = binsim.late_convolvers[n].process(input_mono)

                left = np.sum([left_ds, left_early, left_late], axis=0)
                right = np.sum([right_ds, right_early, right_late], axis=0)

                # Sum results from all convolvers
                if n == 0:
                    binsim.result[:, 0] = left
                    binsim.result[:, 1] = right
                else:
                    binsim.result[:, 0] = np.add(binsim.result[:, 0], left)
                    binsim.result[:, 1] = np.add(binsim.result[:, 1], right)

            # Finally apply Headphone Filter
            if callback.config.get('useHeadphoneFilter'):
                binsim.result[:, 0], binsim.result[:, 1], _ = \
                    binsim.convolverHP.process(*binsim.input_BufferStereo.process(binsim.result))


        # Scale data
        # binsim.result = np.divide(binsim.result, float((amount_channels) * 2))
        binsim.result = np.multiply(binsim.result, callback.config.get('loudnessFactor')/float((amount_channels) * 2))


        outdata[:, 0] = binsim.result[:, 0]
        outdata[:, 1] = binsim.result[:, 1]
        
        # Report buffer underrun
        if status == 4:
            binsim.log.warn('Output buffer underrun occurred')

        # Report clipping
        if np.max(np.abs(binsim.result)) > 1:
            binsim.log.warn('Clipping occurred: Adjust loudnessFactor!')

        cb_end = timeit.default_timer()
        cb_time = cb_end - cb_start
        # binsim.log.info(f'Audio callback took {cb_time * 1000} ms.')
        binsim.cb_time_usage[1] = cb_time/(binsim.blockSize/binsim.sampleRate)*100
        binsim.cb_time_usage = np.roll(binsim.cb_time_usage, 1, axis=0)
        if count % binsim.cpu_usage_update_rate == 0:
            binsim.log.info(f'Audio callback utilization Mean: {np.mean(binsim.cb_time_usage)} % - Max: {np.max(binsim.cb_time_usage)}')

    callback.config = binsim.config

    return callback
