#!/usr/local/bin/python3

import logging
from pyvantage import Vantage

_LOGGER = logging.getLogger(__name__)

logging.basicConfig(level=logging.DEBUG)

name_mappings = {}
name_mappings['main house'] = 'MH'
name_mappings['pool house'] = 'PH'
name_mappings['guest house'] = 'GH'
name_mappings['upper floor'] = 'U'
name_mappings['main floor'] = 'M'
name_mappings['basement'] = 'B'
name_mappings['outside'] = 'OUT'
name_mappings['0-10v relays'] = True # means to skip

#v = Vantage('24.130.56.81', '', '')
#v = Vantage('oar.mine.nu', 'greg', 'vantage', None, None, 3001, 2001, name_mappings)
v = Vantage('192.168.0.3', 'greg', 'vantage', None, None, 3001, 2001, name_mappings)
v.load_xml_db(False) # set to True to disable the cache
v.connect()
ll = v._vid_to_load[3496]

ll.level = 50
print("ll = ", ll)
print(ll.support_color_temp)
print(ll.support_color)
ll = v._vid_to_load[3388]
print("ll = ", ll)
print(ll.support_color_temp)
print(ll.support_color)
ll = v._vid_to_load[3474]
print("ll = ", ll)
print(ll.support_color_temp)
print(ll.support_color)
v.set_variable_vid(2722, "foo from pyvantage")
v.set_variable_vid(2721, 7)
v.set_variable_vid(2721, "fasdfaosd")
v.set_variable_vid(2720, "fasdfaosd")
print(v.outputs)
sh = v._vid_to_shade[3036]
print("shade = ", sh)
sh.open()
import time
time.sleep(8)


for output in v.outputs:
    area = v._vid_to_area[output.area]
    print(output)
    print(area)
    
