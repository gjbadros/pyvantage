#!/usr/local/bin/python3

# creates groups-new.yaml, customize.yaml
DEPRECATED USE hass-make-groups.pl

import logging
import time
import argparse
import re
from collections import defaultdict
from pyvantage import Vantage

parser = argparse.ArgumentParser()
parser.add_argument("-v", "--verbose", help="increase output verbosity",
                    action="store_true")
args = parser.parse_args()

_LOGGER = logging.getLogger(__name__)

logging.basicConfig(level=logging.INFO)


name_mappings = {}
name_mappings['main house'] = 'MH'
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
v = Vantage('oar.mine.nu', 'greg', 'vantage', None, None, 3001, 2001, name_mappings)
v.load_xml_db(False) # set to True to disable the cache
v.connect()

# compute an entity name for home assistant from the output load
def ha_name_from_output(o):
    name = o.name.lower()
    name = name.replace('-','')
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
    if o.name.startswith("Station Load "):
        return False
    if o.is_output() and o._output_type == 'SKIP RELAY':
        return False
    return True


prefixes = {
    "gh-": ("guest house", []),
    "ph-": ("pool house", []),
    "mh-b-": ("mh basement", []),
    "mh-m-": ("mh main", []),
    "mh-u-": ("mh upstairs", []),
    "office-": ("office", [])
    }
    

e_to_output_sn = {}

def process_loads(p,loads):
    answer = defaultdict(list) # room_name -> list of entities in that room
    for o in loads:
        # FIXME: use better heuristic
        if 'spare' in o.name.lower():
            continue
        if not include_load(o):
            continue
        if not o.name.lower().startswith(p):
            continue
        p1 = re.compile(r'^.*-\s*')
        short_name = p1.sub('', o.name)
        p2 = re.compile(r'\s*\(.*?\)')
        short_name = p2.sub('', short_name)
        p3 = re.compile(r'^\d+\s*')
        short_name = p3.sub('', short_name)
        if o.name.count("-") >= 2:
            (house,floor,room_name) = o.name.split("-",2)
            room_name = re.compile(r'\s*-.*$').sub('', room_name)
            room_name = room_name.lower()
        else:
            _LOGGER.warning("Unknown room name for " + o.name)
            continue
        ha_name = ha_name_from_output(o)
        answer[room_name].append(ha_name_from_output(o))
        e_to_output_sn[ha_name] = (o, short_name.title())
    return answer

g = open('groups-new.yaml', 'w')
c = open('customize.yaml', 'w')

print('''
trackers:
  view: yes
  entities: device_tracker.amazon9054d2271,device_tracker.hide_if_away

locks:
  view: yes
  entities: sensor.assa_abloy_unknown_type8002_id0600_access_control, sensor.assa_abloy_unknown_type8002_id0600_alarm_level_2, ensor.assa_abloy_unknown_type8002_id0600_alarm_type_2
  

important_people:
  name: Important People
  view: no
  entities:
    - device_tracker.pixel_2_xl
    - device_tracker.404e36850a4d
''', file=g)

for p,pv in prefixes.items():
    rn_to_entities_list = process_loads( p, v.outputs)
    print(pv[0] + ":", file=g)
    print("  view: yes", file=g)
    print("  entities:", file=g)
    for rn,e in rn_to_entities_list.items():
        gn = "group." + pv[0] + "__" + rn
        gn = gn.replace(" ", "_")
        print("    - " + gn, file=g)
    print("", file=g)
    for rn,es in rn_to_entities_list.items():
        gn = pv[0] + "__" + rn
        gn = gn.replace(" ", "_")
        print(gn + ":", file=g)
        print("  name: " + rn, file=g)
        print("  entities: ", ",".join(es), file=g)
        print("\n", file=g)
        for e in es:
            print(e + ":", file=c)
            fn = e_to_output_sn[e][1]
            print("  friendly_name:", fn, file=c)


g.close()
c.close()
