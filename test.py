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
import time
import traceback

from pyvantage import Vantage

_LOGGER = logging.getLogger(__name__)

logging.basicConfig(level=logging.DEBUG)

def parse_args():
    parser = argparse.ArgumentParser("pyvantage")

    parser.add_argument('--host', action='store', dest='host',
                        help="Host to connect to in")
    parser.add_argument('--sleep-for', action='store', type=int, dest='sleep_for',
                        help="Sleep and monitor events for some number of seconds before exiting")
    parser.add_argument('--use-cache', action='store_true', dest='use_cache',
                        help="Use cache instead of refetching config from host")
    parser.add_argument('--parse-file', action='store', dest='dc_filename',
                        help="Just parse the file instead of connecting to host")
    parser.add_argument('--run-one-test', action='store_true', dest='run_one_test',
                        help="Run single test")
    parser.add_argument('--run-tests', action='store_true', dest='run_tests',
                        help="Run various tests to demonstrate some functonality")
    parser.add_argument('--run-new-tests', action='store_true', dest='run_new_tests',
                        help="Run various tests to demonstrate some functonality")
    parser.add_argument('--get-levels-test', action='store_true', dest='get_levels_test',
                        help="Run various tests to demonstrate some functonality")
    parser.add_argument('--user', action='store', dest='user',
                        help='Username for logging in')
    parser.add_argument('--password', action='store', dest='password',
                        help='Password for that user')
    parser.add_argument('--dump-outputs', action='store_true', dest='dump_outputs',
                        help='Display all outputs after parsing')
    parser.add_argument('--dump-buttons', action='store_true', dest='dump_buttons',
                        help='Display all buttons after parsing')
    parser.add_argument('--num-connections', action='store', dest='num_connections',
                        help='Number of command processing connections to use')
    parser.add_argument('--use-ssl', action='store_true', dest='use_ssl')

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
    ls = v._vid_to_sensor[3371] # light sensor
    ls.update()
    los = v._vid_to_sensor[429] # power sensor
    los.update()
    time.sleep(2)
    print("ls = " + str(ls.value))
    print("los = " + str(los.value))
    ll = v._vid_to_load[4727]
    print("ll = " + str(ll))
    ll.level = 100
    ll.rgb = [100,20,20]
    ulg = v._vid_to_load[4727] # mbr uplight group
    ulg.level = 100
    ulg.hs = [0,100]

    print("desk light")
    dwl = v._vid_to_load[3497]
    dwl.level = 0
    time.sleep(6)
    dwl.hs = [355, 100]
    dwl.level = 100
    time.sleep(6)
    dwl.level = 0
    dwl.hs = [180, 100]
    print("changed color while off")
    time.sleep(2)
    dwl.level = 100
    time.sleep(6)
    print("desk light rgb")
    dwl = v._vid_to_load[3497]
    dwl.level = 0
    time.sleep(6)
    dwl.rgb = [255,0,0]
    dwl.level = 100
    time.sleep(6)
    dwl.level = 0
    dwl.rgb = [0,0,255]
    print("changed color while off")
    time.sleep(2)
    dwl.level = 100

def run_new_tests(v):
    while True:
      ll = v._vid_to_load[3467]
      ll.level = 90-ll.level
      time.sleep(3)



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
    name_mappings['0-10v relays'] = True  # means to skip

    args = parse_args()

    if args.dc_filename is not None:
        v = Vantage(None, None, None, filename=args.dc_filename, file_port=2010, cmd_port=3010, use_ssl=True)
        try:
            f = open(args.dc_filename, "r")
            xml_db = f.read()
            f.close()
            _LOGGER.info("read vantage configuration file %s",
                         args.dc_filename)
            v.do_parse(xml_db)
        except Exception as e:
            traceback.print_exc()
            _LOGGER.warning("Failed loading cached config: %s",
                            e)
        return

    v = Vantage(args.host, args.user, args.password, None, None,
                3010 if args.use_ssl else 3001, 2010 if args.use_ssl else 2001, name_mappings, None, True,
                int(args.num_connections) if args.num_connections else 1, use_ssl=args.use_ssl)
    v.load_xml_db(not args.use_cache)
    v.connect()
    time.sleep(2)

    if args.run_tests:
        various_tests(v)

    if args.run_new_tests:
        run_new_tests(v)

    if args.run_one_test:
        print("bonus bed ball -- lifx via virtual dmx")
        bbb = v._vid_to_load[4536]
        bbb.level = 50
        time.sleep(2)

        bbb.hs = (56, 20)
        time.sleep(3)
        bbb.rgb = (255, 30, 70)
        time.sleep(3)
        bbb.color_temp = 2000
        time.sleep(3)
        bbb.color_temp = 4000

    if args.get_levels_test:
        for vid in [3442, 3455, 3456, 3457, 3458, 3459, 3462, 3463, 3468, 3469, 3470, 3471, 3472, 3473, 3474, 3477, 3479, 3481, 3482, 3483, 3484, 3485, 3486, 3487, 3488, 3489, 3500, 3502, 3503, 3504, 3505, 3506, 3507, 3508, 3509, 3510, 3552, 3553, 3554, 3555, 3556, 3557, 3558, 3559, 3729, 3730, 3736, 4388, 4395, 4506, 4507, 4508, 4523, 4524, 4525, 4526, 4527, 4528, 4529, 4536, 4625, 4626, 4627, 4634, 4722, 4727, 5320, 5844, 5846, 5848, 5850, 5852, 5855, 6180, 6181, 6184, 6185, 6186, 6187, 6188, 6189, 6190, 6191, 6192, 6193, 6194, 6195, 6196, 6199, 7029, 7030, 7033, 7034, 7035, 7036, 7037, 7166, 7167]:
            _LOGGER.info("%s has level %s", vid, v._vid_to_load[vid].level)

    if args.sleep_for:
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
