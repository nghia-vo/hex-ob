#!/usr/bin/env python3
import os
import re
from collections import defaultdict

from caproto import (ChannelChar, ChannelData, ChannelDouble, ChannelEnum,
                     ChannelInteger, ChannelString)
from caproto.server import ioc_arg_parser, PVGroup, run

# HEX-sim addition: prefixes owned by a per-detector sim (e.g. the Kinetix
# caproto sim) that the blackhole must NOT answer, so it doesn't shadow their
# correctly-typed PVs. Space/comma-separated, via BLACKHOLE_EXCLUDE_PREFIXES.
_EXCLUDE_PREFIXES = tuple(
    p for p in os.environ.get("BLACKHOLE_EXCLUDE_PREFIXES", "").replace(",", " ").split()
    if p
)


def _is_excluded(key):
    return any(key.startswith(p) for p in _EXCLUDE_PREFIXES)

# HEX-sim addition: the single asyn port name every fabricated PortName/
# NDArrayPort PV reports (see the comment at the fabrication site).
SIM_ASYN_PORT = os.environ.get("BLACKHOLE_ASYN_PORT", "SIM1")

PLUGIN_TYPE_PVS = [
    (re.compile('image\\d:'), 'NDPluginStdArrays'),
    (re.compile('Stats\\d:'), 'NDPluginStats'),
    (re.compile('CC\\d:'), 'NDPluginColorConvert'),
    (re.compile('Proc\\d:'), 'NDPluginProcess'),
    (re.compile('Over\\d:'), 'NDPluginOverlay'),
    (re.compile('ROI\\d:'), 'NDPluginROI'),
    (re.compile('Trans\\d:'), 'NDPluginTransform'),
    (re.compile('netCDF\\d:'), 'NDFileNetCDF'),
    (re.compile('TIFF\\d:'), 'NDFileTIFF'),
    (re.compile('JPEG\\d:'), 'NDFileJPEG'),
    (re.compile('Nexus\\d:'), 'NDPluginNexus'),
    (re.compile('HDF\\d:'), 'NDFileHDF5'),
    (re.compile('Magick\\d:'), 'NDFileMagick'),
    (re.compile('Current\\d:'), 'NDPluginStats'),
    (re.compile('SumAll'), 'NDPluginStats'),
]


class ReallyDefaultDict(defaultdict):
    def __contains__(self, key):
        # Explicitly-seeded PVs (e.g. a per-detector typed PV set) always win.
        if dict.__contains__(self, key):
            return True
        # Don't claim PVs owned by a separate per-detector sim (let it answer).
        if _is_excluded(key):
            return False
        return True

    def __missing__(self, key):
        if _is_excluded(key):
            raise KeyError(key)
        if (key.endswith('-SP') or key.endswith('-I') or
                key.endswith('-RB') or key.endswith('-Cmd')):
            key, *_ = key.rpartition('-')
            return self[key]
        if key.endswith('_RBV') or key.endswith(':RBV'):
            return self[key[:-4]]
        ret = self[key] = self.default_factory(key)
        return ret

class BlackholeIOC(PVGroup):
    """
    IOC that spoofs a beamline.

    You can set up SubGroups for beamline components that interact with each other.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(prefix="", *args, **kwargs)
        # Copy the original pvdb so we can use it for channels
        self.old_pvdb = self.pvdb.copy()
        # Reset the pvdb to use our fabricate_channel function
        self.pvdb = ReallyDefaultDict(self.fabricate_channel)

    def fabricate_channel(self, key):
        # Use existing channels if they exist
        if key in self.old_pvdb:
            return self.old_pvdb[key]
        if 'PluginType' in key:
            for pattern, val in PLUGIN_TYPE_PVS:
                if pattern.search(key):
                    return ChannelString(value=val)
        # One consistent asyn port for every driver/plugin: legacy ophyd's
        # validate_asyn_ports requires a plugin's NDArrayPort value to name an
        # existing driver's PortName. Fabricating each as its own PV name (the
        # upstream behaviour) makes every port unique, so the port graph never
        # links and any classic-ophyd AreaDetector device fails validation.
        # (Same flaw exists upstream in NSLS2/test-beamline-profiles.)
        elif 'ArrayPort' in key:
            return ChannelString(value=SIM_ASYN_PORT)
        elif 'PortName' in key:
            return ChannelString(value=SIM_ASYN_PORT)
        elif 'EnableCallbacks' in key:
            return ChannelEnum(value=0, enum_strings=['Disabled', 'Enabled'])
        elif 'BlockingCallbacks' in key:
            return ChannelEnum(value=0, enum_strings=['No', 'Yes'])
        elif 'Auto' in key:
            return ChannelEnum(value=0, enum_strings=['No', 'Yes'])
        elif 'ImageMode' in key:
            return ChannelEnum(value=0, enum_strings=['Single', 'Multiple', 'Continuous'])
        elif 'WriteMode' in key:
            return ChannelEnum(value=0, enum_strings=['Single', 'Capture', 'Stream'])
        elif 'ArraySize' in key:
            return ChannelData(value=10)
        elif 'TriggerMode' in key:
            return ChannelEnum(value=0, enum_strings=['Internal', 'External'])
        elif 'FileWriteMode' in key:
            return ChannelEnum(value=0, enum_strings=['Single'])
        elif 'FilePathExists' in key:
            return ChannelData(value=1)
        elif 'WaitForPlugins' in key:
            return ChannelEnum(value=0, enum_strings=['No', 'Yes'])
        elif ('file' in key.lower() and 'number' not in key.lower() and
            'mode' not in key.lower()):
            return ChannelChar(value='a' * 250)
        elif ('filenumber' in key.lower()):
            return ChannelInteger(value=0)
        elif 'Compression' in key:
            return ChannelEnum(value=0, enum_strings=['None', 'N-bit', 'szip', 'zlib', 'blosc'])
        elif key.endswith(".EGU"):
            return ChannelString(value="mm")
        return ChannelDouble(value=0.0)


def main():
    print('''
*** WARNING ***
This script spawns an EPICS IOC which responds to ALL caget, caput, camonitor
requests.  As this is effectively a PV black hole, it may affect the
performance and functionality of other IOCs on your network.

The script ignores the --interfaces command line argument, always
binding only to 127.0.0.1, superseding the usual default (0.0.0.0) and any
user-provided value.
*** WARNING ***

Press return if you have acknowledged the above, or Ctrl-C to quit.''')

    try:
        input()
    except KeyboardInterrupt:
        print()
        return
    print('''

                         PV blackhole started

''')
    _, run_options = ioc_arg_parser(
        default_prefix='',
        desc="PV black hole")
    run_options['interfaces'] = ['127.0.0.1']
    run(BlackholeIOC().pvdb,
        **run_options)


if __name__ == '__main__':
    main()
