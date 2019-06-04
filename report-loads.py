#!/usr/local/bin/python3

import logging
import time
import argparse
from pyvantage import Vantage

parser = argparse.ArgumentParser()
parser.add_argument("-v", "--verbose", help="increase output verbosity",
                    action="store_true")
parser.add_argument("-F", "--really_flash", help="actually flash the loads on and off",
                    action="store_true")
parser.add_argument("-s", "--skipvids", help="comma separated list of vids to leave alone")
parser.add_argument("-l", "--include_lifx", help="do include lifx power loads",
                    action="store_true")
parser.add_argument("-r", "--include_relays", help="do include relays in the loads we check",
                    action="store_true")
parser.add_argument("-c", "--onlycolor", help="only include loads with COLOR in name",
                    action="store_true")
args = parser.parse_args()

_LOGGER = logging.getLogger(__name__)

#logging.basicConfig(level=logging.INFO)
#logging.basicConfig(level=logging.DEBUG)
logging.basicConfig(level=logging.WARNING)

really_flash = args.really_flash
#really_flash = False

skip_vids = {}
if args.skipvids is not None:
    skip_vids = set(args.skipvids.split(",")) # { 386, 397 }
    if args.verbose:
        _LOGGER.info("Skipping vids = " + str(skip_vids))
skip_station_loads = True
skip_hvr_loads = not args.include_relays


name_mappings = {}
name_mappings['main house'] = 'MH'
name_mappings['pool house'] = 'PH'
name_mappings['guest house'] = 'GH'
name_mappings['upper floor'] = 'U'
name_mappings['main floor'] = 'M'
name_mappings['basement'] = 'B'
name_mappings['outside'] = 'OUT'
#name_mappings['0-10v relays'] = True # means to skip

#v = Vantage('24.130.56.81', '', '')
v = Vantage('oar.mine.nu', 'greg', 'vantage', None, None, 3001, 2001, name_mappings)
#v = Vantage('192.168.0.3', 'greg', 'vantage', None, None, 3001, 2001, name_mappings)
v.load_xml_db(False) # set to True to disable the cache
v.connect()

# LVOSPWM/JoinTable positions 1-4 describe what
# Loads with <Parent Position-"5-8">LVOWPWM_VID</Parent> do
# and map to <Parent Position="9-12">LVOWPWM_VID</Parent>

def include_load(o):
    if args.onlycolor and not 'COLOR' in o.name:
        return False
    if "lifx" in o.name.lower() and not args.include_lifx:
        _LOGGER.info("skipping lifx load: %s", o)
        return False
    if o.vid in skip_vids:
        return False
    if skip_station_loads and o.name.startswith("Station Load "):
        return False
    if skip_hvr_loads and o._load_type == 'RELAY':
        return False
    if o._output_type == 'SKIP RELAY':
        return False
    return True
    

def report_load(outputs):
    for o in outputs:
        print(o.level, o)

report_load(list(filter(lambda o: o.is_output() and include_load(o),
                      v.outputs)))
