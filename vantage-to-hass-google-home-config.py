#!/usr/local/bin/python3

import logging
import time
import argparse
import re
from pyvantage import Vantage

parser = argparse.ArgumentParser()
parser.add_argument("-v", "--verbose", help="increase output verbosity",
                    action="store_true")
args = parser.parse_args()

_LOGGER = logging.getLogger(__name__)

logging.basicConfig(level=logging.INFO)


name_mappings = {}
name_mappings['main house'] = 'MH'
name_mappings['office'] = 'MHO'
name_mappings['pool house'] = 'PH'
name_mappings['guest house'] = 'GH'
name_mappings['upper floor'] = 'U'
name_mappings['main floor'] = 'M'
name_mappings['basement'] = 'B'
name_mappings['outside'] = 'OUT'
name_mappings['0-10v relays'] = True # means to skip

rev_nm = {}
for (k,v) in name_mappings.items():
    rev_nm[v] = k



#v = Vantage('24.130.56.81', '', '')
v = Vantage('192.168.0.3', 'greg', 'vantage', None, None, 3001, 2001, name_mappings)
v.load_xml_db(False) # set to True to disable the cache
v.connect()

print ('''
#google_assistant:
  project_id: ha-oar
  api_key: !secret ga_api_key
  entity_config:
''')

# compute an entity name for home assistant from the output load
def ha_name_from_output(o):
    name = o.name.lower()
    name = name.replace('-','_')
    name = name.replace(' ','_')
    name = name.replace('(','')
    name = name.replace(')','')
    name = name.replace('#','')
    name = 'light.' + name
    _LOGGER.debug("replaced " + o.name + " to " + name)
    return name

def include_load(o):
    if ' COLOR' in o.name:
        return False
    if "lifx" in o.name.lower():
        _LOGGER.info("skipping lifx load: %s", o)
        return False
    if "gone" in o.name.lower():
        _LOGGER.info("skipping load marked gone: %s", o)
        return False
    if o.name.startswith("Station Load "):
        return False
    if o._output_type == 'SKIP RELAY':
        return False
    return True

room_names = dict()

def output_load(loads):
    for o in loads:
        name = ha_name_from_output(o)
        # FIXME: use better heuristic
        if 'spare' in name.lower():
            continue
        if not include_load(o):
            continue
        print("    " + name + ":")
        p1 = re.compile(r'^.*-\s*')
        short_name = p1.sub('', o.name)
        p2 = re.compile(r'\s*\(.*?\)')
        short_name = p2.sub('', short_name)
        p3 = re.compile(r'^\d+\s*')
        short_name = p3.sub('', short_name)
        print("      # " + o.name)
        print("      name: " + short_name)
        if o.name.count("-") >= 2:
            (house,floor,room_name) = o.name.split("-",2)
            if "MHO" == house:
                room_name = floor
            room_name = re.compile(r'\s*-.*$').sub('', room_name)
            room_name = re.compile(r'bed$', re.I).sub('bedroom', room_name)
            room_name = room_name.title()
            print("      room: '" + room_name + "'")
            room_names[room_name] = room_names.get(room_name, 0) + 1
        else:
            _LOGGER.warning("Unknown room name for " + o.name)
        
    
output_load(list(filter(lambda o: o.is_output(), v.outputs)))

_LOGGER.info("room_names = " + str(room_names))
