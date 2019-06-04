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
v = Vantage('oar.mine.nu', 'greg', 'vantage', None, None, 3001, 2001, name_mappings)
#v = Vantage('192.168.0.3', 'greg', 'vantage', None, None, 3001, 2001, name_mappings)
v.load_xml_db(False) # set to True to disable the cache
v.connect()
ll1 = v._vid_to_load[3486] #RGBW Ceiling light in basement lounge
print("ll1 = ", ll1)
print(ll1.support_color_temp)
print(ll1.support_color)
print(ll1.level)

ll2 = v._vid_to_load[3388]  # Single channel DMX light at basement spa sink
print("ll2 = ", ll2)
print(ll2.support_color_temp)
print(ll2.support_color)
print(ll2.level)

ll3 = v._vid_to_load[3475]  # South DW two channel DMX light in home theatre
print("ll3 = ", ll3)
print(ll3.support_color_temp)
print(ll3.support_color)
print(ll3.level)


ll4 = v._vid_to_load[90]  # color_temp usai west cans light (not dmx)
print("ll4 = ", ll4)
print(ll4.support_color_temp)
print(ll4.support_color)
print(ll4.level)

ll5 = v._vid_to_load[494]  # playroom center pendants not color temp, just dimmable
print("ll5 = ", ll5)
print(ll5.support_color_temp)
print(ll5.support_color)
print(ll5.level)


print("initially: ", ll1.rgb)
ll1.rgb = [30, 40, 50]
ll1.hs = [90, 40]
print("then: ", ll1.rgb)
print("then: ", ll1.hs)

import time
time.sleep(8)

print("finally: ", ll1.rgb)
