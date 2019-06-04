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
v.call_task_vid(20)    
v.call_task_vid("20")
v.call_task_vid(3637)
v.call_task_vid("3637")
v.call_task(5)
v.call_task("foo")
v.call_task("DR OFF")
