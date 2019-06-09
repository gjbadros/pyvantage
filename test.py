#!/usr/local/bin/python3
# Copyright (C) 2019, Greg J. Badros <badros@gmail.com>
# See LICENSE.  Use at your own risk!
#
# You can break your Vantage system or components connected
# to your vantage system by using this code.
#
# TODO: handle name mappings via configuration instead of code

import argparse
import logging

from pyvantage import Vantage

_LOGGER = logging.getLogger(__name__)

logging.basicConfig(level=logging.DEBUG)

def parse_args():
    parser = argparse.ArgumentParser("pyvantage")

    parser.add_argument('--host', required=True, action='store', dest='host',
                        help="Host to connect to in")
    parser.add_argument('--sleep-for', action='store', type=int, dest='sleep_for',
                        help="Sleep and monitor events for some number of seconds before exiting")
    parser.add_argument('--use-cache', action='store_true', dest='use_cache',
                        help="Use cache instead of refetching config from host")
    parser.add_argument('--run-tests', action='store_true', dest='run_tests',
                        help="Run various tests to demonstrate some functonality")
    parser.add_argument('--user', action='store', dest='user',
                        help='Username for logging in')
    parser.add_argument('--password', action='store', dest='password',
                        help='Password for that user')
    parser.add_argument('--dump-outputs', action='store_true', dest='dump_outputs',
                        help='Display all outputs after parsing')
    parser.add_argument('--dump-buttons', action='store_true', dest='dump_buttons',
                        help='Display all buttons after parsing')

    results = parser.parse_args()
    return results

def various_tests(v):
    """TODO: rewrite this to do something useful."""
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
    

def main():
    """Connect to a vantage server, parse config, and monitor events."""
    name_mappings = {}
    name_mappings['main house'] = 'MH'
    name_mappings['pool house'] = 'PH'
    name_mappings['guest house'] = 'GH'
    name_mappings['upper floor'] = 'U'
    name_mappings['main floor'] = 'M'
    name_mappings['basement'] = 'B'
    name_mappings['outside'] = 'OUT'
    name_mappings['0-10v relays'] = True # means to skip
    
    args = parse_args()

    v = Vantage(args.host, args.user, args.password, None, None, 3001, 2001, name_mappings)
    v.load_xml_db(not args.use_cache)
    v.connect()

    if args.run_tests:
        various_tests(v)
    
    import time
    time.sleep(args.sleep_for)
    
    if args.dump_outputs:
        for output in v.outputs:
            area = v._vid_to_area[output.area]
            print(output)
            print(area)

    if args.dump_buttons:
        for b in v.buttons:
            area = v._vid_to_area[b.area]
            print(b)
            print(area)
    

if __name__ == '__main__':
    main()
                  
