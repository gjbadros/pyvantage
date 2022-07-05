"""
Vantage Controller module for interacting with the infusion controller via TCP.
Basic operations for enumerating and controlling the loads are supported.

Author: Greg J. Badros

Originally based on pylutron which was written by Dima Zavin

See also https://www.npmjs.com/package/vantage-infusion
and https://github.com/angeloxx/homebridge-vantage/blob/master/index.js

To use with home assistant and its virtual python environment, you need to:

$ cd .../path/to/home-assistant/
$ pip3 install --upgrade .../path/to/pyvantage

Then the component/vantage.py and its require line will work.

For development, I do:

$ docker-shell homeassistant
> cd /usr/local/lib/python3.7/site-packages/pyvantage/
> cp /config/pyvantage/pyvantage/_init__.py .
# or
> cp /config/pyvantage/pyvantage/__init__.py /usr/local/lib/python3.7/site-packages/pyvantage
# where /config in the docker image points to my home assistant config directory
# which has a pyvantage subdirectory containing a clone of the github repo.

"""

__Author__ = "Greg J. Badros"
__copyright__ = "Copyright 2018, 2019, 2020 Greg J. Badros"

# USAGE:
#
# Instantiate a Vantage controller object:
#
#   vc = Vantage("192.168.1.42", "myusername", "mypassword");
#
# Load the XML configuration file for the controller.  First tries to read
# cache from local disk, if not available retrieves it from controller.  This
# is then parsed to generate a Python object for every object with a VID on
# the controller.  (See below for mapping from Vantage to pyvantage objects.)
#
#   vc.load_xml_db();
#
# Decide which objects you want to hear status updates for.  For example, this
# registers a handler to learn about all changes to controlled loads:
#
#   for output in vc.outputs:
#     vc.subscribe(output, my_update_handler)
#
# Open a connection to the controller.  Spawns a second thread, which is
# responsible for all communication with the Vantage and invokes update
# handlers when state changes occur.
#
#   vc.connect();
#
#
# Vantage Object    | pyvantage object | Output
# ------------------+------------------+--------------
# Area              | Area             | Used to give names to other objects
# IRZone            | Area             | Used to give names to other objects
# Load              | Output           | vc.outputs (light, switch, or fan)
# DDGColorLoad      | Output           | vc.outputs
# LoadGroup         | LoadGroup        | vc.outputs, vc.load_groups
# Keypad            | Keypad           | vc.keypads
# DualRelayStation  | Keypad           | vc.keypads
# IRZone            | Keypad           | vc.keypads
# Dimmer            | Keypad           | vc.keypads
# EqCtrl            | Keypad           | vc.keypads
# EqUx              | Keypad           | vc.keypads
# Button            | Button           | vc.buttons
# DryContact        | Button           | vc.buttons
# GMem              | Variable         | vc.variables
# OmniSensor        | OmniSensor       | vc.sensors
# LightSensor       | LightSensor      | vc.sensors
# Task              | Task             | vc.tasks
# MechoShade        | Shade            | vc.outputs
# QISBlind          | Shade            | vc.outputs
# BlindGroup        | Shade            | vc.outputs
# QMotion           | Shade            | vc.outputs
# Somfy             | Shade            | vc.outputs
#
# Config file objects which are not parsed by this module include:
#     AreaFragment, BackBox, Category, Elk.M1_*, Enclosure, EthernetLink,
#     FixtureDefinition, ButtonStyle, EqUXStyle, KeypadStyle, LEDStyle, Master,
#     Module, ModuleGen2, DCPowerProfile, PowerProfile, PWMPowerProfile,
#     Schedule, Script, SerialPort, StationPunch, Timer, User, UserGroup,
#     WireLink, CameraWidget, LightingWidget, MediaWidget, SceneWidget,
#     SecurityWidget, TimerWidget.
#
# Things that Vantage can do which are not yet supported:
#
# - Beep keypads. (There no direct support for this via TCP interface
#   that I can see.  It appears the beep function is implemented from
#   lower-level primitives in the XML config file.)
#
# - Change button colors.
#
# - Detect double/triple/quadruple presses on buttons, or long
#   presses. (This is also implemented from lower-level primitives in
#   the XML config.  Clients of this library just have to count press
#   and release events themselves).
#
# - Control devices connected via serial/ethernet links, such as Elk alarms,
#   stereo systems, etc.
#
#
#  light.mh_m_great_room_big_ass_fan (load_type == Motor)

import logging
import telnetlib
import socket
import select
import threading
import time
import base64
import re
import json
import os
import traceback

from collections import deque
from xml.sax.saxutils import escape

from colormath.color_objects import sRGBColor, HSVColor
from colormath.color_conversions import convert_color


def kelvin_to_level(kelvin):
    """Convert kelvin temperature to a USAI level."""
    if kelvin < 2200:
        return 0
    if kelvin > 6000:
        return 100.0
    return (kelvin-2200)/(6000-2200) * 100


def level_to_kelvin(level):
    """Convert a level to a kelvin temperature."""
    if level < 0:
        return 2200
    if level > 100:
        return 6000
    return (6000-2200) * level/100 + 2200


def level_to_mireds(level):
    """Convert a level to mired color temperature."""
    kelvin = level_to_kelvin(level)
    mireds = 1000000/kelvin
    return mireds


_LOGGER = logging.getLogger(__name__)


class VantageException(Exception):
    """Top level module exception."""


class VIDExistsError(VantageException):
    """Asserted when registerering a duplicate integration id."""


class ConnectionExistsError(VantageException):
    """Raised when a connection already exists (e.g. two connect() calls)."""


class VantageConnection(threading.Thread):
    """Encapsulates the connection to the Vantage controller."""

    def __init__(self, host, user, password, cmd_port, recv_callback,
                 commdebug=True, num_connections=2):
        """Initializes the vantage connection, doesn't actually connect."""
        threading.Thread.__init__(self, name="VantageConnection")

        self._host = host
        self._user = user
        self._password = password
        self._cmd_port = cmd_port
        self._num_connections = num_connections
        self._telnet = [None] * num_connections
        self._connected = [False] * num_connections
        self._iconn = 0  # index into the _telnet array
        self._lock = threading.RLock()
        self._connect_cond = threading.Condition(lock=self._lock)
        self._recv_cb = recv_callback
        self._done = False
        self._commdebug = commdebug

        self.setDaemon(True)

    def connect(self):
        """Connects to the vantage controller."""
        if all(self._connected) or self.is_alive():
            raise ConnectionExistsError("Already connected")
        # After starting the thread we wait for it to post us
        # an event signifying that connection is established. This
        # ensures that the caller only resumes when we are fully connected.
        self.start()  # ultimately calls run()
        with self._lock:
            _LOGGER.debug("Waiting for all connections")
            self._connect_cond.wait_for(lambda: all(self._connected))
            _LOGGER.debug("All connected!")

    # VantageConnection
    def _send_ascii_nl_locked(self, cmd, i):
        """Sends the specified command to the vantage controller.
        Assumes lock is held."""
        if self._commdebug:
            if cmd.startswith("LOGIN"):
                pass
            elif cmd.startswith("GET") or cmd.startswith("ADDSTATUS"):
                _LOGGER.debug("Vantage #%s send_ascii_nl: %s", i, cmd)
            else:
                _LOGGER.info("Vantage #%s send_ascii_nl: %s", i, cmd)
        try:
            self._telnet[i].write(cmd.encode('ascii') + b'\r\n')
        except BrokenPipeError:
            _LOGGER.warning("Vantage BrokenPipeError - disconnected but retrying")
            self._connected[i] = False
            raise

    def send_ascii_nl(self, cmd):
        """Sends the specified command to the vantage controller.

        Must not hold self._lock"""
        with self._lock:
            self._send_ascii_nl_locked(cmd, self._iconn)
            if not cmd.startswith("GET"):
                self._iconn = (self._iconn + 1) % self._num_connections

    def _do_login_locked(self, i):
        """Executes the login procedure (telnet) as well as setting up some
        connection defaults like turning off the prompt, etc."""
        while True:
            try:
                self._telnet[i] = telnetlib.Telnet(self._host, self._cmd_port)
                break
            except Exception as e:
                _LOGGER.warning("Could not connect #%s to %s:%d, "
                                "retrying after 3 sec (%s)", i,
                                self._host, self._cmd_port,
                                e)
                time.sleep(3)
                continue
        if not (self._user is None or self._password is None):
            _LOGGER.debug("Connection #%s is made, logging in", i)
            self._send_ascii_nl_locked("LOGIN " + self._user +
                                       " " + self._password, i)
            _LOGGER.debug("reading login response for #%s", i)
            self._telnet[i].read_until(b'\r\n', 2)
        if i == 0:
            self._send_ascii_nl_locked("STATUS LOAD", i)
            self._telnet[i].read_until(b'\r\n', 2)
            self._send_ascii_nl_locked("STATUS BLIND", i)
            self._telnet[i].read_until(b'\r\n', 2)
            self._send_ascii_nl_locked("STATUS BTN", i)
            self._telnet[i].read_until(b'\r\n', 2)
            self._send_ascii_nl_locked("STATUS VARIABLE", i)
            self._telnet[i].read_until(b'\r\n', 2)
        return True

    def _disconnect_locked(self):
        self._connected = [False] * self._num_connections
        self._connect_cond.notify_all()
        self._telnet = [None] * self._num_connections
        _LOGGER.warning("Disconnected")

    def _maybe_reconnect(self):
        """Reconnects to controller if we have been previously disconnected."""
        need_notify = False
        with self._lock:
            for i in range(0, self._num_connections):
                if not self._connected[i]:
                    _LOGGER.info("Connecting #%s to %s", i, self._host)
                    self._do_login_locked(i)
                    self._connected[i] = True
                    need_notify = True
                    _LOGGER.info("Connected #%s", i)
            if need_notify:
                _LOGGER.debug("maybe_reconnect: notify_all")
                self._connect_cond.notify_all()

    def run(self):
        """Main thread to maintain connection and receive remote status."""
        _LOGGER.debug("VantageConnection run started")
        while True:
            self._maybe_reconnect()
            try:
                sockets = [t.get_socket() for t in self._telnet]
                readable, _, exceptional = select.select(sockets, [], [])
                for i, t in enumerate(self._telnet):
                    if t.get_socket() in exceptional:
                        _LOGGER.error("Exceptional socket #%s: %s", i, t)
                        raise EOFError()
                    if t.get_socket() in readable:
                        line = self._telnet[i].read_until(b"\n", 2)
                        self._recv_cb(line.decode('ascii').rstrip(), i)
            except EOFError:
                _LOGGER.warning("run got EOFError")
                with self._lock:
                    self._disconnect_locked()
                continue
            except BrokenPipeError:
                _LOGGER.warning("run got BrokenPipeError")
                with self._lock:
                    self._disconnect_locked()
                continue


def _desc_from_t1t2(title1, title2):
    if not title2:
        desc = title1 or ''
    else:
        desc = title1 + ' ' + title2
    return desc.strip()


def replace_keep_case(word, replacement, text):
    """Replace word with replacement in text.
    While preserving the case (lower/upper/title) of word."""
    def func(match):
        g = match.group()
        if g.islower():
            return replacement.lower()
        if g.istitle():
            return replacement.title()
        if g.isupper():
            return replacement.upper()
        return replacement
    return re.sub(word, func, text, flags=re.I)


class VantageXmlDbParser():
    """The parser for Vantage XML database.

    The database describes all the rooms (Area), keypads (Device), and switches
    (Output). We handle the most relevant features, but some things like LEDs,
    etc. are not implemented."""

    def __init__(self, vantage, xml_db_str):
        """Initializes the XML parser from raw XML data as string input."""
        self._vantage = vantage
        self._xml_db_str = xml_db_str
        self.outputs = []
        self.variables = []
        self.tasks = []
        self.buttons = []
        self.keypads = []
        self.sensors = []
        self.load_groups = []
        self.last_area_vid = -1
        self.vid_to_area = {}
        self.vid_to_load = {}
        self.vid_to_keypad = {}
        self.vid_to_button = {}
        self.vid_to_variable = {}
        self.vid_to_task = {}
        self.vid_to_sensor = {}
        self.name_to_task = {}
        self.vid_to_shade = {}
        self._name_area_to_vid = {}
        self._vid_to_colorvid = {}
        self.project_name = None

    def parse(self):
        """Main entrypoint into the parser.

        It interprets and creates all the relevant Vantage objects and
        stuffs them into the appropriate hierarchy.
        """

        import xml.etree.ElementTree as ET

        root = ET.fromstring(self._xml_db_str)
        # The structure of a Lutron config is something like this:
        # <Areas>
        #   <Area ...>
        #     <DeviceGroups ...>
        #     <Scenes ...>
        #     <ShadeGroups ...>
        #     <Outputs ...>
        #     <Areas ...>
        #       <Area ...>
        # Vantage uses a flatter style with elements that are:
        # Area (with @VID and <Name> and <Area> (parent VID) )
        # Load (with @VID and <Name> and <Area> (enclosing Area VID))
        # GMem (with @VID and <Name> [variables])
        # Task (with @VID and <Name> )
        # OmniSensor (with @VID and <Name> )
        # Timer (with @VID and <Name> )
        # Keypad (with @VID and <Name> )

        areas = root.findall(".//Objects//Area[@VID]")
        for area_xml in areas:
            if self.project_name is None:
                self.project_name = area_xml.find('Name').text
                _LOGGER.debug("Set project name to %s", self.project_name)
            area = self._parse_area(area_xml)
            _LOGGER.debug("Area = %s", area)
            self.vid_to_area[area.vid] = area
            self.last_area_vid = area.vid

        irzones = root.findall(".//Objects//IRZone[@VID]")
        for irzone_xml in irzones:
            area = self._parse_irzone(irzone_xml)
            _LOGGER.debug("IRZone = %s", area)
            self.vid_to_area[area.vid] = area

        loads = root.findall(".//Objects//Load[@VID]")
        loads = loads + root.findall(".//Objects//Vantage.DDGColorLoad[@VID]")
        other_loads = []
        color_loads = []
        open_loads = []
        for ld in loads:
            t = ld.find('Name').text
            if t.endswith(' COLOR'):
                color_loads.append(ld)
            elif t.lower().endswith(' open'):
                open_loads.append(ld)
            else:
                other_loads.append(ld)
        ordered_loads = open_loads + other_loads + color_loads
        skip_load_vids = set()
        for load_xml in ordered_loads:
            xml_name = load_xml.find('Name').text
            output = None
            if xml_name.lower().endswith(" open"):
                close_name = replace_keep_case(' open', " close", xml_name)
                stop_name = replace_keep_case(' open', " stop", xml_name)
                isopen_name = replace_keep_case(' open', " is open", xml_name)
                _LOGGER.debug("Looking for close_name = %s", close_name)
                _LOGGER.debug("Looking for stop_name = %s", stop_name)
                _LOGGER.debug("Looking for isopen_name = %s", isopen_name)
                close_load_xml = root.findall(
                    ".//Objects//Load[Name='" + close_name + "']")
                if len(close_load_xml) == 1:
                    close_load_xml = close_load_xml[0]
                    isopen_contact_xml = root.findall(
                        ".//Objects//DryContact[Name='" + isopen_name + "']")
                    if len(isopen_contact_xml) == 1:
                        isopen_contact_xml = isopen_contact_xml[0]
                    else:
                        isopen_contact_xml = None
                    stop_load_xml = root.findall(
                        ".//Objects//Load[Name='" + stop_name + "']")
                    if len(stop_load_xml) == 1:
                        stop_load_xml = stop_load_xml[0]
                    else:
                        stop_load_xml = None
                    shade = self._parse_3_shade(isopen_contact_xml,
                                                load_xml,
                                                close_load_xml,
                                                stop_load_xml)
                    for v in shade.vids:
                        skip_load_vids.add(v)
                    self.vid_to_shade[shade.vid] = shade
                    self.outputs.append(shade)
                    _LOGGER.debug("shade3 = %s", shade)
                    continue

            if int(load_xml.get("VID")) in skip_load_vids:
                _LOGGER.debug("Skipping %s because used for blind3", load_xml)
                continue
            output = self._parse_output(load_xml)
            if output is None:
                continue
            self.outputs.append(output)
            self.vid_to_load[output.vid] = output
            _LOGGER.debug("Output = %s", output)
            self.vid_to_area[output.area].add_output(output)

        load_groups = root.findall(".//Objects//LoadGroup[@VID]")
        for lg_xml in load_groups:
            lgroup = self._parse_load_group(lg_xml)
            if lgroup is None:
                continue
            self.load_groups.append(lgroup)
            self.outputs.append(lgroup)
            self.vid_to_load[lgroup.vid] = lgroup
            _LOGGER.debug("load group = %s", lgroup)
            self.vid_to_area[lgroup.area].add_output(lgroup)

        keypads = root.findall(".//Objects//Keypad[@VID]")
        keypads = keypads + root.findall(".//Objects//DualRelayStation[@VID]")
        keypads = keypads + root.findall(".//Objects//IRZone[@VID]")
        keypads = keypads + root.findall(".//Objects//Dimmer[@VID]")
        keypads = keypads + root.findall(".//Objects//EqCtrl[@VID]")
        keypads = keypads + root.findall(".//Objects//EqUX[@VID]")
        for kp_xml in keypads:
            keypad = self._parse_keypad(kp_xml)
            _LOGGER.debug("keypad = %s", keypad)
            self.vid_to_keypad[keypad.vid] = keypad
            if keypad.area > 0:
                self.vid_to_area[keypad.area].add_keypad(keypad)
            self.keypads.append(keypad)

        buttons = root.findall(".//Objects//Button[@VID]")
        for button_xml in buttons:
            b = self._parse_button(button_xml)
            if not b:
                continue
            _LOGGER.debug("b = %s", b)
            self.vid_to_button[b.vid] = b
            if b.area != -1:
                self.vid_to_area[b.area].add_button(b)
                self.buttons.append(b)

        drycontacts = root.findall(".//Objects//DryContact[@VID]")
        for dc_xml in drycontacts:
            dc = self._parse_drycontact(dc_xml)
            if not dc:
                continue
            _LOGGER.debug("dc = %s", dc)
            self.vid_to_button[dc.vid] = dc
            self.buttons.append(dc)

        variables = root.findall(".//Objects//GMem[@VID]")
        for v in variables:
            var = self._parse_variable(v)
            _LOGGER.debug("var = %s", var)
            self.vid_to_variable[var.vid] = var
            # N.B. variables have categories, not areas, so no add to area
            self.variables.append(var)

        omnisensors = root.findall(".//Objects//OmniSensor[@VID]")
        for s in omnisensors:
            sensor = self._parse_omnisensor(s)
            _LOGGER.debug("sensor = %s", sensor)
            self.vid_to_sensor[sensor.vid] = sensor
            # N.B. variables have categories, not areas, so no add to area
            self.sensors.append(sensor)

        lightsensors = root.findall(".//Objects//LightSensor[@VID]")
        for s in lightsensors:
            sensor = self._parse_lightsensor(s)
            _LOGGER.debug("sensor = %s", sensor)
            self.vid_to_sensor[sensor.vid] = sensor
            # N.B. variables have categories, not areas, so no add to area
            self.sensors.append(sensor)

        tasks = root.findall(".//Objects//Task[@VID]")
        for t in tasks:
            task = self._parse_task(t)
            _LOGGER.debug("task = %s", task)
            self.vid_to_task[task.vid] = task
            self.name_to_task[task.name] = task
            # N.B. tasks have categories, not areas, so no add to area
            self.tasks.append(task)

        # Lots of different shade types, one xpath for each kind of shade
        # MechoShade driver shades
        shades = \
            root.findall(".//Objects//MechoShade.IQ2_Shade_Node_CHILD[@VID]")
        shades = (shades +
                  root.findall(".//Objects//MechoShade.IQ2_Group_CHILD[@VID]"))
        # Native QIS QMotion shades
        shades = shades + root.findall(".//Objects//QISBlind[@VID]")
        shades = shades + root.findall(".//Objects//BlindGroup[@VID]")
        # Non-native QIS Driver QMotion shades (the old way)
        shades = (shades +
                  root.findall(".//Objects//QMotion.QIS_Channel_CHILD[@VID]"))
        # Somfy radio-controlled
        shades = (shades +
                  root.findall(".//Objects//Somfy.URTSI_2_Shade_CHILD[@VID]"))
        # Somfy RS-485 SDN wired shades
        shades = (shades +
                  root.findall(".//Objects//Somfy.RS-485_Shade_CHILD[@VID]"))

        for shade_xml in shades:
            shade = self._parse_shade(shade_xml)
            if shade is None:
                continue
            self.vid_to_shade[shade.vid] = shade
            self.outputs.append(shade)
            _LOGGER.debug("shade = %s", shade)

        _LOGGER.debug("self._name_area_to_vid = %s", self._name_area_to_vid)

        return True

    def _parse_area(self, area_xml):
        """Parses an Area tag, which is effectively a room, depending on how the
        Vantage controller programming was done."""
        try:
            vid = int(area_xml.get('VID'))
            area = Area(self._vantage,
                        name=area_xml.find('Name').text,
                        parent=int(area_xml.find('Area').text),
                        vid=vid,
                        note=area_xml.find('Note').text)
            return area
        except Exception as e:
            _LOGGER.warning("Error parsing Area vid = %d: %s", vid, e)

    def _parse_irzone(self, irzone_xml):
        """Parses an IRZone tag, which we treat like an area with no parent."""
        try:
            vid = int(irzone_xml.get('VID'))
            irzone = Area(self._vantage,
                          name=irzone_xml.find('Name').text,
                          parent=0,
                          vid=vid,
                          note=irzone_xml.find('Note').text)
            return irzone
        except Exception as e:
            _LOGGER.warning("Error parsing IRZone vid = %d: %s", vid, e)

    def _parse_variable(self, var_xml):
        """Parses a variable (GMem) tag."""
        try:
            vid = int(var_xml.get('VID'))
            subtype_node = var_xml.find('Tag')
            subtype = ''
            if subtype_node is not None:
                subtype = subtype_node.text.lower()
            var = Variable(self._vantage,
                           name=var_xml.find('Name').text,
                           vid=vid, subtype=subtype)
            return var
        except Exception as e:
            _LOGGER.warning("Error parsing variable vid = %d: %s", vid, e)

    def _parse_omnisensor(self, sensor_xml):
        """Parses an OmniSensor tag."""
        try:
            vid = int(sensor_xml.get('VID'))
            kind = {
                "Power": "power",
                "Current": "current",
                "Temperature": "sensor"
            }[sensor_xml.find('Model').text]
            sensor = OmniSensor(self._vantage,
                                name=sensor_xml.find('Name').text,
                                kind=kind,
                                vid=int(sensor_xml.get('VID')))
            return sensor
        except Exception as e:
            _LOGGER.warning("Error parsing omnisensor vid = %d: %s", vid, e)

    def _parse_lightsensor(self, sensor_xml):
        """Parses a LightSensor object."""
        try:
            vid = int(sensor_xml.get('VID'))
            area_xml = sensor_xml.find('Area')
            area = (area_xml is not None and int(area_xml.text)) or -1
            value_range = (float(sensor_xml.find('RangeLow').text),
                           float(sensor_xml.find('RangeHigh').text))
            return LightSensor(self._vantage,
                               name=sensor_xml.find('Name').text,
                               area=area,
                               value_range=value_range,
                               vid=vid)
        except Exception as e:
            _LOGGER.warning("Error parsing lightsensor vid = %d: %s", vid, e)

    def _parse_shade(self, shade_xml):
        """Parses a shade node.

        Either a MechoShade.IQ2_Shade_Node_CHILD or
        QMotion.QIS_Channel_CHILD (shade) tag.
        """
        try:
            vid = int(shade_xml.get('VID'))
            area_xml = shade_xml.find('Area')
            area_vid = self.last_area_vid
            if area_xml is not None:
                area_vid = int(area_xml.text)

            shade = Shade(self._vantage,
                          name=shade_xml.find('Name').text,
                          area_vid=area_vid,
                          vid=vid)
            return shade
        except Exception as e:
            _LOGGER.warning("Error parsing shade vid = %d: %s", vid, e)

    def _parse_output(self, output_xml):
        """Parses a load.

        A load is generally one or more lights/outlets/etc. which can be
        switched and possibly dimmed by the controller.

        """
        try:
            vid = int(output_xml.get('VID'))
            dname_xml = output_xml.find('DName')
            out_name = dname_xml is not None and dname_xml.text
            if out_name:
                out_name = out_name.strip()
            if not out_name or out_name.isspace():
                out_name = output_xml.find('Name').text.strip()
            area_xml = output_xml.find('Area')
            area_vid = self.last_area_vid
            if area_xml is not None:
                area_vid = int(area_xml.text)

            area_name = self.vid_to_area[area_vid].name.strip()
            lt_xml = output_xml.find('LoadType')
            if lt_xml is not None:
                load_type = lt_xml.text.strip()
            else:
                load_type = output_xml.find('ColorType').text.strip()

            output_type = 'LIGHT'

            # TODO: find a better heuristic so that on/off lights still show up
            if (load_type == 'High Voltage Relay' or
                load_type == 'Low Voltage Relay'):
                output_type = 'RELAY'

            if ' COLOR' in out_name and load_type != 'HID':
                _LOGGER.warning("Load %s [%d] might be color load "
                                "but of type %s not HID",
                                out_name, vid, load_type)

            if load_type == 'HID':
                output_type = 'COLOR'
                omit_trailing_color_re = re.compile(r'\s+COLOR\s*$')
                load_name = omit_trailing_color_re.sub("", out_name)
                _LOGGER.debug("Found HID Type, guessing load name is %s",
                              load_name)

                load_vid = self._name_area_to_vid.get((load_name, area_vid))
                if load_vid:
                    self._vid_to_colorvid[load_vid] = vid
                    _LOGGER.debug("Found colorvid = %d for load_vid %d"
                                  " (names %s and %s) in area %s (%d)",
                                  vid, load_vid, out_name, load_name,
                                  area_name, area_vid)
                    self.vid_to_load[load_vid].color_control_vid = vid
                else:

                    # TODO: do not assume that the regular loads are
                    # handled before the COLOR loads
                    _LOGGER.warning("Could not find matching load for "
                                    "COLOR load %s (%d) in area %s (%d)",
                                    out_name, vid, area_name, area_vid)

            # it's a DMX color load if and only if it's RGB or RGBW loadtype
            # and Channel2 is nonempty
            # (we represent dynamic white as a R+B (no green) RGB load,
            # and that only support_color_temp)
            dmx_color = False
            if load_type.startswith("RGB"):
                ch1 = output_xml.find('Channel1')
                ch2 = output_xml.find('Channel2')
                ch3 = output_xml.find('Channel3')
                # _LOGGER.debug("ch1 = %s, ch2 = %s", ch1.text, ch2.text)
                if not(ch1.text and ch1.text.strip() != ""):
                    _LOGGER.warning("RGB* load with missing Channel1: %s",
                                    out_name)
                if not(ch3.text and ch3.text.strip() != ""):
                    _LOGGER.warning("RGB* load with missing Channel3: %s",
                                    out_name)
                if load_type == "RGBW":
                    if not(ch2.text and ch2.text.strip() != ""):
                        _LOGGER.warning("RGBW load with missing Channel2: %s",
                                        out_name)
                    dmx_color = True
                else:   # load_type == "RGB"
                    if ch2.text and ch2.text.strip() != "":
                        dmx_color = True
                    else:
                        # just a dynamic white red/blue light
                        # (just two shades of white, really)
                        load_type = "DW"

            if output_type == 'LIGHT':
                self._name_area_to_vid[(out_name, area_vid)] = vid
            output = Output(self._vantage,
                            name=out_name,
                            area=area_vid,
                            output_type=output_type,
                            load_type=load_type,
                            cc_vid=(load_vid if output_type == 'COLOR'
                                    else self._vid_to_colorvid.get(vid)),
                            dmx_color=dmx_color,
                            vid=vid)
            return output
        except Exception as e:
            _LOGGER.warning("Error parsing Output vid = %d: %s", vid, e)

    def _parse_3_shade(self, isopen_xml, open_xml, close_xml, stop_xml):
        """Parses three XML elements that together make a single shade.
        open_xml is the output load low-voltage relay for opening,
        close_xml is the output load low-voltage relaying for closing.
        isopen_xml is the drycontact for reading whether it is open."""
        _LOGGER.debug("_parse_3_shade io,o,c,s=%s, %s, %s, %s",
                      isopen_xml, open_xml, close_xml, stop_xml)
        vids = [int(isopen_xml.get('VID')) if isopen_xml else None,
                int(open_xml.get('VID')),
                int(close_xml.get('VID')),
                int(stop_xml.get('VID')) if stop_xml else None]

        shade_name = open_xml.find('Name').text.strip()[:-5]
        area_xml = open_xml.find('Area')
        area_vid = self.last_area_vid
        if area_xml is not None:
            area_vid = int(area_xml.text)

        close_area_xml = close_xml and close_xml.find('Area')
        close_area_vid = self.last_area_vid
        if close_area_xml is not None:
            close_area_vid = int(close_area_xml.text)

        isopen_area_xml = isopen_xml and isopen_xml.find('Area')
        isopen_area_vid = self.last_area_vid
        if isopen_area_xml is not None:
            isopen_area_vid = int(isopen_area_xml.text)

        stop_area_xml = stop_xml and stop_xml.find('Area')
        stop_area_vid = self.last_area_vid
        if stop_area_xml is not None:
            stop_area_vid = int(stop_area_xml.text)

        if ((area_vid != close_area_vid) or
             (isopen_xml and area_vid != isopen_area_vid) or
             (stop_xml and area_vid != stop_area_vid)):
            _LOGGER.warning("open/close/stop/isopen device "
                            "areas do not match: %s", shade_name)
            return None
        shade = Shade3(self._vantage,
                       name=shade_name,
                       area_vid=area_vid,
                       vids=vids)
        return shade

    def _parse_load_group(self, output_xml):
        """Parses a load group, which is a set of loads"""
        out_name = output_xml.find('DName').text
        if out_name:
            out_name = out_name.strip()
        if not out_name or out_name.isspace():
            out_name = output_xml.find('Name').text
        else:
            _LOGGER.debug("Using dname = %s", out_name)
        area_xml = output_xml.find('Area')
        area_vid = self.last_area_vid
        if area_xml is not None:
            area_vid = int(area_xml.text)

#        area_name = self.vid_to_area[area_vid].name
        loads = output_xml.findall('./LoadTable/Load')
        vid = output_xml.get('VID')
        vid = int(vid)

        load_vids = []
        color_vids = []
        dmx_color = False
        support_color_temp = False
        for load in loads:
            v = int(load.text)
            load_vids.append(v)
            if self.vid_to_load[v]._dmx_color:
                dmx_color = True
                support_color_temp = True
                color_vids.append(v)
                _LOGGER.debug("for loadgroup %d, vid %s supports color",
                              vid, v)
            elif self.vid_to_load[v].support_color_temp:
                support_color_temp = True
                color_vids.append(v)
                _LOGGER.debug("for loadgroup %d, vid %s supports color_temp",
                              vid, v)

        output = LoadGroup(self._vantage,
                           name=out_name,
                           area=area_vid,
                           load_vids=load_vids,
                           color_vids=color_vids,
                           dmx_color=dmx_color,
                           support_color_temp=support_color_temp,
                           vid=vid)
        return output

    def _parse_keypad(self, keypad_xml):
        """Parses a keypad device."""
        area_xml = keypad_xml.find('Area')
        area_vid = int(area_xml.text) if area_xml else -1
        keypad = Keypad(self._vantage,
                        name=keypad_xml.find('Name').text + ' [K]',
                        area=area_vid,
                        vid=int(keypad_xml.get('VID')))
        return keypad

    def _parse_task(self, task_xml):
        """Parses a task object."""
        task = Task(self._vantage,
                    name=task_xml.find('Name').text,
                    vid=int(task_xml.get('VID')))
        return task

    def _parse_drycontact(self, dc_xml):
        """Parses a dry contact switch."""
        # A dry contact switch *may* be plugged into the back of a keypad (and
        # hence has a keypad like a button does), but nobody cares if it does.
        # A dry contact in other respects acts like a button, so treat it as
        # one.
        try:
            vid = int(dc_xml.get('VID'))
            if self.vid_to_shade.get(vid):
                _LOGGER.debug("Skipping vid=%d as drycontact "
                              "because already part of a BLIND3", vid)
                return None
            name = dc_xml.find('Name').text + ' [C]'
            parent = dc_xml.find('Parent')
            parent_vid = int(parent.text)
            area_xml = dc_xml.find('Area')
            area_vid = int(area_xml.text) if area_xml else -1
            num = 0
            keypad = None
            _LOGGER.debug("Found DryContact with vid = %d", vid)
            # Ugh, awful -- three different ways of representing bad-value
            button = Button(self._vantage, name, area_vid, vid, num,
                            parent_vid, keypad, False)
            return button
        except Exception as e:
            _LOGGER.warning("Error parsing drycontact vid = %d: %s",
                            vid, e)
            traceback.print_exc()

    def _parse_button(self, button_xml):
        """Parses a button device that part of a keypad."""
        try:
            vid = int(button_xml.get('VID'))
            xml_name = button_xml.find('Name')
            name = ""
            if xml_name is not None:
                name = xml_name.text.strip()
                # By default Design Center names each button on a
                # keypad "Button 1", "Button 2", etc.  This is not
                # useful.  So if a user has those names, treat it as
                # no name:
                if name.startswith("Button "):
                    name = ""
            if not name:
                # You *can* give each button on each keypad a name in
                # Design Center, but why would you bother?  If no name
                # is present, just use the descriptive text which
                # appears on the actual button:
                xml_name = button_xml.find("Text1")
                if xml_name is None:
                    return None
                xml_text2 = button_xml.find("Text2")
                text1 = xml_name.text or ""
                text2 = xml_text2.text or ""
                name = text1.strip() + ' ' + text2.strip()
            name += ' [B]'
            # no Text1 sub-element on DryContact
            parent = button_xml.find('Parent')
            parent_vid = int(parent.text)
            text1 = button_xml.find('Text1').text
            text2 = button_xml.find('Text2').text
            desc = _desc_from_t1t2(text1, text2)
            num = int(parent.get('Position'))
            keypad = self.vid_to_keypad.get(parent_vid)
            if keypad is None:
                irzone = self.vid_to_area.get(parent_vid)
                if irzone is None:
                    _LOGGER.debug("No parent vid = %d for button vid = %d "
                                  "(leaving button out)",
                                  parent_vid, vid)
                    return None

                button = Button(self._vantage, name, irzone.vid, vid, num,
                                parent_vid, keypad, desc)

            else:
                area = keypad.area
                button = Button(self._vantage, name, area, vid, num,
                                parent_vid, keypad, desc)
                keypad.add_button(button)
            return button
        except Exception as e:
            _LOGGER.warning("Error parsing button vid = %d: %s",
                            vid, e)
            traceback.print_exc()



# Connect to port 2001 and write
# "<IBackup><GetFile><call>Backup\\Project.dc</call></GetFile></IBackup>"
# to get a Base64 response of the last XML file of the designcenter config.
# Then use port 3001 to send commands.

# maybe need
# <ILogin><Login><call><User>USER</User><Password>PASS</Password></call></Login></ILogin>


class Vantage():
    """Main Vantage Controller class.

    This object owns the connection to the controller, the rooms that
    exist in the network, handles dispatch of incoming status updates,
    etc.

    """

    # See vantage host commands reference
    # (you may need to be a dealer/integrator for access)
    # Response lines come back from Vantage with this prefix
    OP_RESPONSE = 'R:'
    # Status report lines come back from Vantage with this prefix
    OP_STATUS = 'S:'

    def __init__(self, host, user, password,
                 only_areas=None, exclude_areas=None,
                 cmd_port=3001, file_port=2001,
                 name_mappings=None, filename=None,
                 commdebug=True, num_connections=1):
        """Initializes the Vantage object. No connection is made to the remote
        device."""
        self._host = host
        self._user = user
        self._password = password
        self._name = None
        if self._host is not None:
            self._conn = VantageConnection(host, user, password, cmd_port,
                                           self._recv, commdebug,
                                           num_connections)
        else:
            self._conn = None
            if filename is None:
                raise Exception("Need host or filename to be specified")
        self._cmds = deque([])
        self._name_mappings = name_mappings
        self._file_port = file_port
        self._only_areas = only_areas
        self._exclude_areas = exclude_areas
        self._ids = {}
        self._names = {}   # maps from unique name to id
        self._subscribers = {}
        self._vid_to_area = {}  # copied out from the parser
        self._vid_to_load = {}  # copied out from the parser
        self._vid_to_variable = {}  # copied out from the parser
        self._vid_to_task = {}  # copied out from the parser
        self._vid_to_shade = {}  # copied out from the parser
        self._vid_to_sensor = {}  # copied out from the parser
        self._name_to_task = {}  # copied out from the parser
        self._colorvid_to_group_vid = {}
        self._brightnessvid_to_group_vid = {}
        self._r_cmds = ['LOGIN', 'LOAD', 'STATUS', 'GETLOAD', 'GETVARIABLE',
                        'ERROR',
                        'TASK', 'GETBLIND', 'BLIND', 'INVOKE', 'VARIABLE',
                        'GETLIGHT', 'GETPOWER', 'GETCURRENT',
                        'GETSENSOR', 'ADDSTATUS', 'DELSTATUS',
                        'GETCUSTOM', 'RAMPLOAD']
        self._s_cmds = ['LOAD', 'TASK', 'BTN', 'VARIABLE', 'BLIND', 'STATUS']
        self.outputs = None
        self.variables = None
        self.tasks = None
        self.buttons = None
        self.keypads = None
        self.sensors = None

    def subscribe(self, obj, handler):
        """Subscribes to status updates of the requested object.

        The handler will be invoked when the controller sends a
        notification regarding changed state. The user can then
        further query the object for the state itself.

        """

        self._subscribers[obj] = handler

    def get_lineage_from_obj(self, obj):
        """Return list of areas for obj, chasing up to top."""
        count = 0
        area = self._vid_to_area.get(obj.area)
        if area is None:
            return []
        answer = [area.name]
        while area and count < 10:
            count += 1
            parent_vid = area.parent
            if parent_vid == 0:
                break
            area = self._vid_to_area.get(parent_vid)
            if area:
                answer.append(area.name)
        # _LOGGER.debug("lineage for " + str(obj.vid) + " is " + str(answer))
        return answer

    # TODO: cleanup this awful logic
    def register_id(self, cmd_type, cmd_type2, obj, vid=None):
        """Registers an object (through its vid [vantage id]).

        This lets it receive update notifications. This is the core
        mechanism how Output and Keypad objects get notified when the
        controller sends status updates.

        """

        if vid == None:
            vid = obj.vid
        # First, register the VID in our _ids map.  When we issue commands to
        # the Vantage this map lets us route the respones to the correct object
        ids = self._ids.setdefault(cmd_type, {})
        ids = self._ids.setdefault(cmd_type2, {})
        if vid in ids:
            raise VIDExistsError("VID exists %s" % vid)
        self._ids[cmd_type][vid] = obj
        if cmd_type2:
            self._ids[cmd_type2][vid] = obj

        # Now give the object a unique name.  We prefix in reverse order the
        # areas the object is contained in.  So an object may be called "Main
        # Floor-Kitchen-Ceiling Can Lights".  Every object must have a unique
        # name, if there is a duplicate then (VID) is attached to the end.

        lineage = self.get_lineage_from_obj(obj)
        name = ""
        # reverse all but the last element in list
        for n in reversed(lineage[:-1]):
            ns = n.strip()
            if ns.startswith('Station Load '):
                continue
            if ns.startswith('Color Load '):
                continue
            if self._name_mappings:
                mapped_name = self._name_mappings.get(ns.lower())
                if mapped_name is not None:
                    if mapped_name is True:
                        continue
                    ns = mapped_name
            name += ns + "-"

        # TODO: this may be a little too hacky
        # Greg Badros has a convention of naming areas using 2-letter codes.
        # This makes sure that we use "GH-Bedroom High East"
        # instead of "GH-GH Bedroom High East"
        # since it's sometimes convenient to have the short area
        # at the start of the device name in vantage
        if obj.name.startswith(name[0:-1]):
            obj.name = name + obj.name[len(name):]
        else:
            obj.name = name + obj.name

        if obj.name in self._names:
            oldname = obj.name
            obj.name += " (%s)" % (str(obj.vid))
            if ('0-10V RELAYS' in oldname or
                'NOT USED' in oldname or cmd_type == 'BTN'):
                pass
            else:
                _LOGGER.debug("Repeated name `%s' - adding vid to get %s",
                              oldname, obj.name)
        self._names[obj.name] = obj.vid

    # Note: invoked on VantageConnection thread.
    def _recv(self, line, i=0):
        """Invoked by the connection manager to process incoming data."""
        _LOGGER.debug("#%s _recv got line: %s", i, line)
        if line == '':
            return
        typ = None
        # Only handle query response messages, which are also sent on remote
        # status updates (e.g. user manually pressed a keypad button)
        if line[0] == 'R':
            cmds = self._r_cmds
            typ = 'R'
            if self._cmds:
                this_cmd = self._cmds.popleft()
            else:
                this_cmd = "__UNDERFLOW__"
        elif line[0] == 'S':
            cmds = self._s_cmds
            typ = 'S'
        else:
            _LOGGER.error("#%s _recv got unknown line start character", i)
            return
        parts = re.split(r'[ :]', line[2:])
        cmd_type = parts[0]
        vid = parts[1]
        args = parts[2:]
        if cmd_type not in cmds:
            _LOGGER.warning("#%s Unknown cmd %s (%s)", i, cmd_type, line)
            return
        if cmd_type == 'LOGIN':
            _LOGGER.info("#%s login successful", i)
            return
        # TODO: is it okay to ignore R:RAMPLOAD responses?
        # or do we need to handle_update_and_notify like with "LOAD",
        # below
        if line[0] == 'R' and cmd_type in ('STATUS', 'ADDSTATUS',
                                           'DELSTATUS', 'INVOKE',
                                           'GETCUSTOM', 'RAMPLOAD'):
            return
        if line[0] == 'R' and cmd_type == "ERROR":
            _LOGGER.warning("#%s Got %s on command: %s", i, line,
                            this_cmd)
            return
        # is there ever an S:ERROR line? that's all the below covers
        if cmd_type == 'ERROR':
            _LOGGER.error(" #%s _recv got ERROR line: %s", i, line)
            return
        if cmd_type in ('GETLOAD', 'GETPOWER', 'GETCURRENT',
                        'GETVARIABLE', 'GETSENSOR', 'GETLIGHT'):
            cmd_type = cmd_type[3:]  # strip "GET" from front
        elif cmd_type == 'GETBLIND':
            return
        elif cmd_type == 'TASK':
            return
        elif cmd_type == 'VARIABLE':
            _LOGGER.debug("#%s variable set response: %s", i, line)

        ids = self._ids.get(cmd_type)
        if ids is None:
            _LOGGER.warning("#%s Might need to handle cmd_type ids: %s:: %s",
                            i, cmd_type, line)
        else:
            if not vid.isdigit():
                _LOGGER.warning("#%s VID %s is not an integer", i, vid)
                return
            vid = int(vid)
            if vid not in ids:
                _LOGGER.warning("#%s Unknown id %d (%s)", i, vid, line)
                return
            obj = ids[vid]
            # First let the device update itself
            if (typ == 'S' or
                    (typ == 'R' and
                     cmd_type in ('LOAD', 'POWER', 'CURRENT',
                                  'VARIABLE', 'SENSOR', 'LIGHT'))):
                self.handle_update_and_notify(obj, args, vid)

    # Note: invoked on VantageConnection thread.
    def handle_update_and_notify(self, obj, args, vid):
        """Call handle_update for the obj and for subscribers.
        We have to pass the vid along, too, since there are
        object types, e.g., Shade3, that have multiple vids
        represented by a single object and status on any of
        those vids goes back to the same handle_update()."""
        handled = obj.handle_update(args, vid)
        # Now notify anyone who cares that device may have changed
        if handled and handled in self._subscribers:
            self._subscribers[handled](handled)

    def connect(self):
        """Connects to the Vantage controller.

        The TCP connection is used both to send commands and to
        receive status responses.

        """
        self._conn.connect()

    # Vantage
    def send_cmd(self, cmd):
        """Send the host command to the Vantage TCP socket."""
        self._cmds.append(cmd)
        self._conn.send_ascii_nl(cmd)

    # Vantage
    def send(self, op, vid, *args):
        """Formats and sends the command to the controller."""
#    out_cmd = ",".join(
#        (cmd, str(vid)) + tuple((str(x) for x in args)))
        out_cmd = str(vid) + " " + " ".join(str(a) for a in args)
        self.send_cmd(op + " " + out_cmd)

    # TODO: could confirm that this variable exists in the XML we download
    # and/or lookup the variables VID so that we can set it by name
    def set_variable_vid(self, vid, value):
        """Sets variable with vid to value;
        be sure instance type of value is either int or string"""
        num = re.compile(r'^\d+$')
        if isinstance(value, int) or num.match(value):
            self.send_cmd("VARIABLE " + str(vid) + " " + str(value))
        else:
            p = re.compile(r'["\n\r]')
            if p.match(value):
                raise Exception("Newlines and quotes are "
                                "not allowed in Text values")
            self.send_cmd("VARIABLE " + str(vid) +
                          ' "' + value + '"')

    def call_task_vid(self, vid):
        """Call the task with vid."""
        num = re.compile(r'^\d+$')
        if isinstance(vid, int) or num.match(vid):
            task = self._vid_to_task.get(int(vid))
            if task is None:
                _LOGGER.warning("Vid %d is not registered as a task", vid)
            # call it regardless
            self.send_cmd("TASK " + str(vid) + " RELEASE")
            _LOGGER.info("Calling task %s", task)
        else:
            _LOGGER.warning("Could not interpret %d as task vid", vid)

    def call_task(self, name):
        """Call the task with name NAME.
        This is fragile - consider using call_task_vid.

        """
        task = self._name_to_task.get(name)
        if task is not None:
            self.send_cmd("TASK " + str(task.vid) + " RELEASE")
            _LOGGER.info("Calling task %s", task)
        else:
            _LOGGER.warning("No task with name = %s", name)

    def load_xml_db(self, disable_cache=False, config_dir="./"):
        """Load the Vantage database from the server."""
        filename = os.path.join(config_dir, self._host + "_config.txt")
        xml_db = ""
        success = False
        if not disable_cache:
            try:
                f = open(filename, "r")
                xml_db = f.read()
                f.close()
                success = True
                _LOGGER.info("read cached vantage configuration file %s",
                             filename)
            except Exception as e:
                _LOGGER.warning("Failed loading cached config: %s",
                                e)
        if not success:
            _LOGGER.info("doing request for vantage configuration file")
            if disable_cache:
                _LOGGER.info("Vantage config cache is disabled.")
            ts = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ts.connect((self._host, self._file_port))
            if self._user:
                ts.send(("<ILogin><Login><call><User>%s</User>"
                         "<Password>%s</Password>"
                         "</call></Login></ILogin>\n"
                         % (escape(self._user),
                            escape(self._password))).encode("ascii"))
                response = ""
                while not response.endswith("</ILogin>\n"):
                    response += ts.recv(4096).decode('ascii')
                check_return_true = re.compile(r'<return>(.*?)</return>')
                m = check_return_true.search(response)
                if m is None:
                    raise Exception(
                        "Could not find response code from controller "
                        "upon login attempt, response = " + response)
                if m.group(1) != "true":
                    raise Exception("Login failed or not accepted,"
                                    " return code is: " + m.group(1) +
                                    ". Specified user must be in group Admin"
                                    " and have 'Read State', and"
                                    " 'Read Config' permissions.")
            _LOGGER.info("sent GetFile request")
            ts.send("<IBackup><GetFile><call>Backup\\Project.dc"
                    "</call></GetFile></IBackup>\n".encode("ascii"))
            ts.settimeout(1)
            try:
                response = bytearray()
                while True:
                    dbytes = ts.recv(2**20)
                    if not dbytes:
                        break
                    response.extend(dbytes)
            except EOFError:
                ts.close()
                _LOGGER.error("Failed to read vantage configuration file -"
                              " check username and password")
                exit(-1)
            except socket.timeout:
                ts.close()
            _LOGGER.debug("done reading, size = %s", len(response))

            response = response.decode('ascii')
            response = response[response.find("</Result>\n")+10:]
            response = response.replace('<?File Encode="Base64" /', '')
            response = response.replace('?>', '')
            response = response[:response.find('</return>')]
            dbytes = base64.b64decode(response)
            xml_db = dbytes.decode('utf-8')
            if len(xml_db) < 1000:
                _LOGGER.warning("Downloaded short .dc file; "
                                " check saved cache file on disk")
            try:
                f = open(filename, "w")
                f.write(xml_db)
                f.close()
                _LOGGER.info("wrote file %s", filename)
            except Exception as e:
                _LOGGER.warning("could not save %s (%s)",
                                filename, e)

        _LOGGER.info("Loaded xml db")
        # print(xml_db[0:10000])
        self.do_parse(xml_db)

    def do_parse(self, xml_db):
        """Call the parser and copy its output here."""
        parser = VantageXmlDbParser(vantage=self, xml_db_str=xml_db)
        self._vid_to_load = parser.vid_to_load
        self._vid_to_variable = parser.vid_to_variable
        self._vid_to_area = parser.vid_to_area
        self._vid_to_shade = parser.vid_to_shade
        self._name = parser.project_name

        parser.parse()
        self.outputs = parser.outputs
        self.variables = parser.variables
        self.tasks = parser.tasks
        self.buttons = parser.buttons
        self.keypads = parser.keypads
        self.sensors = parser.sensors
        self._vid_to_load = parser.vid_to_load
        self._vid_to_variable = parser.vid_to_variable
        self._vid_to_area = parser.vid_to_area
        self._vid_to_shade = parser.vid_to_shade
        self._vid_to_task = parser.vid_to_task
        self._vid_to_sensor = parser.vid_to_sensor
        self._name_to_task = parser.name_to_task
        self._name = parser.project_name

        _LOGGER.info("Found Vantage project: %s, %d areas, %d loads, "
                     "%d variables, and %d shades",
                     self._name,
                     len(self._vid_to_area.keys()),
                     len(self._vid_to_load.keys()),
                     len(self._vid_to_variable.keys()),
                     len(self._vid_to_shade.keys()))

        return True


class _RequestHelper():
    """A class to help with sending queries to the controller and waiting for
    responses.

    It is a wrapper used to help with executing a user action
    and then waiting for an event when that action completes.

    The user calls request() and gets back a threading.Event on which they then
    wait.

    If multiple clients of a vantage object (eg an Output) want to get a status
    update on the current brightness (output level), we don't want to spam the
    controller with (near)identical requests. So, if a request is pending, we
    just enqueue another waiter on the pending request and return a new Event
    object. All waiters will be woken up when the reply is received and the
    wait list is cleared.

    NOTE: Only the first enqueued action is executed as the assumption is that
    the queries will be identical in nature.
    """

    def __init__(self):
        """Initialize the request helper class."""
        self.__lock = threading.Lock()
        self.__events = []

    def request(self, action):
        """Request an action to be performed, in case one."""
        ev = threading.Event()
        first = False
        with self.__lock:
            if not self.__events:
                first = True
            self.__events.append(ev)
        if first:
            action()
        return ev

    def notify(self):
        """Have all events pending trigger, and reset to []."""
        with self.__lock:
            events = self.__events
            self.__events = []
        for ev in events:
            ev.set()


class VantageEntity:
    """Base class for all the Vantage objects we'd like to manage. Just holds basic
    common info we'd rather not manage repeatedly."""

    def __init__(self, vantage, name, area, vid):
        """Initializes the base class with common, basic data."""
        assert name is not None
        self._vantage = vantage
        self._name = name
        self._area = area
        self._vid = vid
        self._extra_info = {}

    def needs_poll(self):
        """Does not poll by default."""
        return False

    @property
    def name(self):
        """Returns the entity name (e.g. Pendant)."""
        return self._name

    @name.setter
    def name(self, value):
        """Sets the entity name to value."""
        self._name = value

    @property
    def vid(self):
        """The integration id"""
        return self._vid

    @property
    def id(self):
        """The integration id"""
        return self._vid

    @property
    def area(self):
        """The area vid"""
        return self._area

    @property
    def full_lineage(self):
        """Return list of areas for self."""
        areas = []
        avid = self._area
        c = 0
        while True and c < 5:
            c += 1
            area = self._vantage._vid_to_area.get(avid)
            if area is None:
                break
            areas.append(area.name)
            avid = area.parent
            if avid == 0:
                break
        areas = areas[::-1]
        areas.append(self._name)
        return areas

    def handle_update(self, _, __):
        """The handle_update callback is invoked when an event is received
        for the this entity.

        This callback is invoked from the VantageConnection thread.

        Returns:
            self - If event was valid and was handled.
            None - otherwise.
        """
        return None

    @property
    def kind(self):
        """Returns the output type."""
        return self._load_type

    @property
    def extra_info(self):
        """Map of extra info."""
        return self._extra_info

    def is_output(self):
        """Return true iff this is an output."""
        return False


class Area():
    """An area (i.e. a room) that contains devices/outputs/etc."""
    def __init__(self, vantage, name, parent, vid, note):
        self._vantage = vantage
        self._name = name
        self._vid = vid
        self._note = note
        self._parent = parent
        self._outputs = []
        self._keypads = []
        self._buttons = []
        self._sensors = []
        self._variables = []
        self._tasks = []

    def __str__(self):
        """Returns a pretty-printed string for this object."""
        return 'Area name: "%s", vid: %d, parent_vid: %d' % (
            self._name, self._vid, self._parent)

    def add_output(self, output):
        """Adds an output object that's part of this area, only used during
        initial parsing."""
        self._outputs.append(output)

    def add_keypad(self, keypad):
        """Adds a keypad object that's part of this area, only used during
        initial parsing."""
        self._keypads.append(keypad)

    def add_button(self, button):
        """Adds a button object that's part of this area, only used during
        initial parsing."""
        self._buttons.append(button)

    def add_sensor(self, sensor):
        """Adds a motion sensor object that's part of this area, only used during
        initial parsing."""
        self._sensors.append(sensor)

    def add_variable(self, v):
        """Adds a variable object that's part of this area, only used during
        initial parsing."""
        self._variables.append(v)

    def add_task(self, t):
        """Adds a task object that's part of this area, only used during
        initial parsing."""
        self._tasks.append(t)

    @property
    def name(self):
        """Returns the name of this area."""
        return self._name

    @property
    def parent(self):
        """Returns the vid of the parent area."""
        return self._parent

    @property
    def vid(self):
        """The integration id of the area."""
        return self._vid

    @property
    def outputs(self):
        """Return the tuple of the Outputs from this area."""
        return tuple(output for output in self._outputs)

    @property
    def keypads(self):
        """Return the tuple of the Keypads from this area."""
        return tuple(keypad for keypad in self._keypads)

    @property
    def sensors(self):
        """Return the tuple of the MotionSensors from this area."""
        return tuple(sensor for sensor in self._sensors)


class Output(VantageEntity):
    """This is the output entity in Vantage universe. This generally refers to a
    switched/dimmed load, e.g. light fixture, outlet, etc."""
    CMD_TYPE = 'LOAD'
    ACTION_ZONE_LEVEL = 1
    _wait_seconds = 0.03  # TODO:move this to a parameter

    def __init__(self, vantage, name, area, output_type, load_type,
                 cc_vid, dmx_color, vid):
        """Initializes the Output."""
        super(Output, self).__init__(vantage, name, area, vid)
        self._output_type = output_type
        self._load_type = load_type
        self._extra_info['load_type'] = load_type
        self._level = 0.0
        self._color_temp = 2700
        self._is_dimmable = (self._output_type == 'LIGHT' and
                             self._load_type.lower().find("non-dim") == -1)
        self._rgb = [0, 0, 0]
        self._hs = [0, 0]
        # if _load_type == 'COLOR' then _color_control_vid
        # is the load's vid,
        # else it's the color control vid
        self._color_control_vid = cc_vid
        self._dmx_color = dmx_color
        self._query_waiters = _RequestHelper()
        self._ramp_sec = [0, 0, 0]  # up, down, color
        self._vantage.register_id(Output.CMD_TYPE,
                                  "STATUS" if dmx_color else None,
                                  self)
        self._rgb_is_dirty = False
        self._addedstatus = False

    def __str__(self):
        """Returns a pretty-printed string for this object."""
        return (
            "Output name: '%s' area: %d type: '%s' load: '%s' "
            "vid: %d @ %s %s%s%s%s%s [%s]" % (
                self._name, self._area, self._output_type,
                self._load_type, self._vid, self._level,
                ("# " if self._rgb_is_dirty else ""),
                ("(dim) " if self.is_dimmable else ""),
                ("(ctemp) " if self.support_color_temp else ""),
                ("(color) " if self.support_color else ""),
                ("(dirty) " if self._rgb_is_dirty else ""),
                self.full_lineage))

    def __repr__(self):
        """Returns a stringified representation of this object."""
        return str({'name': self._name, 'vid': self._vid, 'area': self._area,
                    'type': self._load_type, 'load': self._load_type,
                    'supports':
                    ("ctemp " if self.support_color_temp else "") +
                    ("color " if self.support_color else "") +
                    ("dirty " if self._rgb_is_dirty else "")})

    @property
    def simple_name(self):
        """Return a simple pretty-printed string for this object."""
        return 'VID:%d (%s) [%s]%s' % (
            self._vid, self._name, self._load_type,
            " [dirty]" if self._rgb_is_dirty else "")

    # ADDSTATUS
    # DELSTATUS
    # S:STATUS [vid] RGBLoad.GetRGB [val] [ch[012]]
    # S:STATUS [vid] RGBLoad.GetRGBW [val] [ch[0123]]
    # S:STATUS [vid] RGBLoad.GetHSL [val] [ch[012]]
    # S:STATUS [vid] RGBLoad.GetColor [value]
    # S:STATUS [vid] RGBLoad.GetColorName [value]
    # INVOKE [vid] RGBLoad.SetRGBW [val0], [val1], [val2], [val3]
    def handle_update(self, args, _):
        """Handles an event update for this object.
        E.g. dimmer level change

        This callback is invoked from the VantageConnection thread.

        """
        _LOGGER.debug("vantage - handle_update %d -- %s", self._vid, args)
        if len(args) == 1:
            level = float(args[0])
            if self._output_type == 'COLOR':
                color_temp = level_to_kelvin(level)
                light = self._vantage._vid_to_load.get(self._color_control_vid)
                if light:
                    light._color_temp = color_temp
                    _LOGGER.debug("Received color change of VID %d "
                                  "set load VID %d to color = %d",
                                  self._vid, self._color_control_vid,
                                  color_temp)
                    light._query_waiters.notify()
                    return light
                _LOGGER.warning("Received color change of VID %d but cannot "
                                "find corresponding load", self._vid)
                return None
            _LOGGER.debug("Updating brightness %d(%s): l=%f",
                          self._vid, self._name, level)
            self._level = level
            # when vantage changes the level itself (e.g., from a keypad)
            # we may have to update the RGB (or RGB_DW) color while processing
            # that status message
            if level > 0 and self._rgb_is_dirty:
                self._invoke_rgb()
            self._query_waiters.notify()
            bvid = self._vantage._brightnessvid_to_group_vid.get(self._vid)
            if bvid:
                group = self._vantage._vid_to_load[bvid]
                _LOGGER.debug("also updating bvid %d(%s): l=%f",
                              bvid, group._name, level)
                group.level = level
                group._query_waiters.notify()
        else:
            if args[0] == 'RGBLoad.GetRGB':
                _LOGGER.info("RGBLoad.GetRGB, handling vid = %d; "
                             "RGBW %s %s",
                             self._vid, args[1], args[2])
                val = int(args[1])
                char = int(args[2])
                if char < 3:
                    self._rgb[char] = val
                if char == 2:
                    self._query_waiters.notify()
                gvid = self._vantage._colorvid_to_group_vid.get(self._vid)
                if gvid:
                    group = self._vantage._vid_to_load[gvid]
                    if char < 3:
                        group._rgb[char] = val
                    if char == 2:
                        group._query_waiters.notify()
        return self

    # It appears that after 64 ADDSTATUS calls, they start
    # failing with ERROR:12 "Failed"
    # If you get that, you need to change num_connections to be > 1
    # so that we open a second (or more) connection to vantage
    # for the additional ADDSTATUS calls since the limit appears to be
    # a per-connection limit.
    # Right now we just round-robin the connections for any non-GET
    # command, and that's a heuristic that works fine for now
    def __do_query_level(self):
        """Helper to perform the actual query the current dimmer level of the
        output. For pure on/off loads the result is either 0.0 or 100.0."""
        if self.support_color and not self._addedstatus:
            self._vantage.send("ADDSTATUS", self._vid)
            self._addedstatus = True
        _LOGGER.debug("getload of %s", self._vid)
        self._vantage.send("GETLOAD", self._vid)

    def last_level(self):
        """Returns last cached value of output level, no query is performed."""
        return self._level

    @property
    def support_color_temp(self):
        """Returns true iff this load can be set to a color temperature."""
        return ((self._color_control_vid is not None) or
                self._load_type == "DW" or
                self._load_type.startswith('RGB'))

    @property
    def support_color(self):
        """Returns true iff this load is full-color."""
        return self._dmx_color

    def _get_level(self):
        """Returns the current output level by querying the controller."""
        ev = self._query_waiters.request(self.__do_query_level)
        ev.wait(self._wait_seconds)
        return self._level

    def _set_level(self, new_level):
        """Sets the new output level."""
        if self._level == new_level:
            return

        self._level = new_level
        _LOGGER.debug("level setter: %s", self)
        if self._rgb_is_dirty:
            self._invoke_rgb()

        if self._is_dimmable:
            if new_level == 0:
                ramp_sec = self._ramp_sec[1]
            else:
                ramp_sec = self._ramp_sec[0]
            self._vantage.send("RAMPLOAD", self._vid, new_level, ramp_sec)
        else:
            self._vantage.send("LOAD", self._vid, new_level)

    level = property(_get_level, _set_level)

    @property
    def rgb(self):
        """Returns current color of the light."""
        return self._rgb

    @rgb.setter
    def rgb(self, new_rgb):
        """Sets new color for the light."""
        if self._rgb == new_rgb:
            if self._rgb_is_dirty:
                self._invoke_rgb()
            return
        # we need to adjust the rgb values to take into account the level
        _LOGGER.debug("%s: rgb = %s", self,
                      json.dumps(new_rgb))
        # INVOKE [vid] RGBLoad.SetRGBW [val0], [val1], [val2], [val3]
        srgb = sRGBColor(*new_rgb)
        hs_color = convert_color(srgb, HSVColor)
        self._hs = [hs_color.hsv_h, hs_color.hsv_s * 100.0]
        self._rgb = new_rgb
        self._rgb_is_dirty = True
        if self._level > 0:
            self._invoke_rgb()
        else:
            self._invoke_hs()

    def _invoke_rgb(self):
        """Update the RGB of the light to self._rgb"""
        (r, g, b) = self._rgb
        ratio = self._level/100
        self._vantage.send("INVOKE", self._vid,
                           ("RGBLoad.SetRGBW %d %d %d %d" %
                            (r*ratio, g*ratio, b*ratio, 0)))
        if self._level > 0:
            self._rgb_is_dirty = False

    @property
    # hue is scaled 0-360, saturation is 0-100
    def hs(self):
        """Returns current HS of the light."""
        return self._hs

    @hs.setter
    def hs(self, new_hs):
        """Sets new Hue/Saturation levels."""
        if self._hs == new_hs:
            return
        _LOGGER.debug("%s: hs = %s", self,
                      json.dumps(new_hs))
        self._hs = new_hs
        hs_color = HSVColor(new_hs[0], new_hs[1]/100.0, 1.0)
        rgb = convert_color(hs_color, sRGBColor)
        self._rgb = [rgb.rgb_r, rgb.rgb_g, rgb.rgb_b]
        self._invoke_hs()

    def _invoke_hs(self):
        """Update the HS of the light to self._hsv
        It's worth noting that HS still specifies a color even when the light is off."""
        (h, s) = self._hs
        self._vantage.send("INVOKE", self._vid,
                           ("RGBLoad.SetHSL %d %d %d" %
                            (h, s, self._level)))

    @property
    def color_temp(self):
        """Returns the current output level by querying the controller."""
        # TODO: query the color temp
#    ev = self._query_waiters.request(self.__do_query_color)
#    ev.wait(self._wait_seconds)
        return self._color_temp

    @color_temp.setter
    def color_temp(self, new_color_temp):
        """Sets the new color temp level."""
        if self._color_temp == new_color_temp:
            return
        if self._dmx_color or self._load_type == "DW":
            _LOGGER.debug("%s: Ignoring call to setter for color_temp "
                          "of dmx_color light", self)
        else:
            self._vantage.send("RAMPLOAD", self._color_control_vid,
                               kelvin_to_level(new_color_temp),
                               self._ramp_sec[2])
        self._color_temp = new_color_temp

# At some later date, we may want to also specify fade and delay times
#  def set_level(self, new_level, fade_time, delay):
#    self._vantage.send(Vantage.OP_EXECUTE, Output.CMD_TYPE,
#        Output.ACTION_ZONE_LEVEL, new_level, fade_time, delay)

    @property
    def color_control_vid(self):
        """Returns the color control vid, if any, for this light."""
        return self._color_control_vid

    @color_control_vid.setter
    def color_control_vid(self, new_ccvid):
        """Sets the color control vid for this light."""
        self._color_control_vid = new_ccvid

    @property
    def kind(self):
        """Returns the output type. At present AUTO_DETECT or NON_DIM."""
        return self._output_type

    @property
    def is_dimmable(self):
        """Returns a boolean of whether or not the output is dimmable."""
        return self._is_dimmable

    def set_ramp_sec(self, up, down, color):
        """Set the ramp speed for load changes, in seconds."""
        self._ramp_sec = [up, down, color]

    def get_ramp_sec(self):
        """Return the current ramp speed settings."""
        return self._ramp_sec

    def is_output(self):
        return True


class VantageSensor(VantageEntity):
    """This is Vantage device that has a value."""

    def __init__(self, vantage, name, area, vid):
        super(VantageSensor, self).__init__(vantage, name, area, vid)
        self._value = None

    @property
    def value(self):
        """The value of the last action of the button."""
        return self._value

    def set_initial_value(self, val):
        """Set the initial value for cases where it is restored."""
        self._value = val


class Shade3(VantageEntity):
    """A shade that is made up of 3 vantage devices.
    1) an open dry contact to initiate opening;
    2) a close dry contact to initiate closing;
    3) a sensor dry contact to tell if it's open.
    An optional 4th dry-contact to stop open/close is allowed."""

    CMD_TYPE = 'BTN'  # for a button -- the isopen sensor

    def __init__(self, vantage, name, area_vid, vids):
        super(Shade3, self).__init__(vantage, name, area_vid, vids[1])
        self._is_open = None
        self._level = None
        self._load_type = 'BLIND3'
        self._extra_info['vids'] = "%s" % vids
        self._isopen_vid = vids[0]
        self._open_vid = vids[1]
        self._close_vid = vids[2]
        self._stop_vid = vids[3]
        self.vids = vids
        if self._isopen_vid:
            self._vantage.register_id(Shade3.CMD_TYPE, None,
                                      self, self._isopen_vid)
        self._vantage.register_id('LOAD', None,
                                  self, self._open_vid)
        self._vantage.register_id('LOAD', None,
                                  self, self._close_vid)
        if self._stop_vid:
            self._vantage.register_id('LOAD', None,
                                      self, self._stop_vid)
        self._query_waiters = _RequestHelper()

    def __str__(self):
        """Returns a pretty-printed string for this object."""
        return (
            "Output3 name: '%s' area: %d type: '%s' is_open: '%s' "
            "vids: %s" % (
                self._name, self._area, self._load_type,
                self._is_open, self.vids))

    def __repr__(self):
        """Returns a stringified representation of this object."""
        return str({'name': self._name, 'vids': self.vids, 'area': self._area,
                    'type': self._load_type, 'is_open': self._is_open})

    @property
    def simple_name(self):
        """Return a simple pretty-printed string for this object."""
        return 'VIDS:%s (%s) [%s]' % (self.vids, self._name, self._load_type)

    def last_level(self):
        """Returns last cached value of output level, no query is performed."""
        return None

    @property
    def level(self):
        """The level (i.e. position) of the shade.
        Shade3 cannot report level, so we just return None"""
        return self._level

    @level.setter
    def level(self, new_level):
        if new_level == 0:
            self.close()
            self._level = 0
        elif new_level == 100:
            self.open()
            self._level = 100

    def __do_query_level(self):
        pass

    def open(self):
        """Open the shade."""
        ev = self._query_waiters.request(self._do_start_open)
        ev.wait(0.5)
        self._vantage.send("LOAD", self._open_vid, "0")
        if not self._isopen_vid:
            self._is_open = True

    def _do_start_open(self):
        """Issue the start open command."""
        self._vantage.send("LOAD", self._open_vid, "100")

    def stop(self):
        """Stop the shade."""
        if self._stop_vid:
            ev = self._query_waiters.request(self._do_start_stop)
            ev.wait(0.5)
            self._vantage.send("LOAD", self._stop_vid, "0")
        else:
            _LOGGER.warning("stop called on blind3 with no stop stupport: %s",
                            self)

    def _do_start_stop(self):
        """Issue the start open command."""
        self._vantage.send("LOAD", self._stop_vid, "100")

    def close(self):
        """Stop the shade."""
        ev = self._query_waiters.request(self._do_start_close)
        ev.wait(0.5)
        self._vantage.send("LOAD", self._close_vid, "0")
        if not self._isopen_vid:
            self._is_open = False

    def _do_start_close(self):
        """Issue the start open command."""
        self._vantage.send("LOAD", self._close_vid, "100")

    def handle_update(self, args, vid):
        """Handle new value for shade3.

        This callback is invoked by the main event loop.

        """
        if vid == self._open_vid:
            _LOGGER.info("Got %s open_vid args = %s", self, args)
            self._query_waiters.notify()
            return self
        if vid == self._close_vid:
            _LOGGER.info("Got %s open_vid args = %s", self, args)
            self._query_waiters.notify()
            return self
        if vid == self._stop_vid:
            _LOGGER.info("Got %s open_vid args = %s", self, args)
            self._query_waiters.notify()
            return self
        if vid != self._isopen_vid:
            _LOGGER.warning("unrecognized vid %d in handle_update: %s",
                            vid, self)
            return None
        if self._isopen_vid is None:
            _LOGGER.warning("Surprised: got handle_update for Blind3 "
                            "without isopen_vid: %s", self)
            return None
        value = args[0]

        if value == "PRESS":
            self._is_open = True
        elif value == "RELEASE":
            self._is_open = False
        else:
            _LOGGER.warning("Got unknown shade3 %s (%d) message: %s",
                            self._name, self._vid, value)
        self._query_waiters.notify()
        return self


class Button(VantageSensor):
    """This object represents a keypad button that we can trigger and handle
    events for (button presses)."""

    CMD_TYPE = 'BTN'  # for a button

    def __init__(self, vantage, name, area, vid, num, parent, keypad, desc):
        super(Button, self).__init__(vantage, name, area, vid)
        self._num = num
        self._parent = parent
        self._keypad = keypad
        self._desc = desc
        self._vantage.register_id(Button.CMD_TYPE, None, self)

    def __str__(self):
        """Pretty printed string value of the Button object."""
        return 'Button name: "%s" num: %d area: %s vid: %d parent: %d [%s]' % (
            self._name, self._num, self._area, self._vid,
            self._parent, self._desc)

    def __repr__(self):
        """String representation of the Button object."""
        return str({'name': self._name, 'num': self._num,
                    'area': self._area, 'vid': self._vid,
                    'desc': self._desc})

    @property
    def kind(self):
        """The type of object (for units in hass)."""
        if self._desc is False:
            return 'contact'
        return 'button'

    @property
    def number(self):
        """Returns the button number."""
        return self._num

    @property
    def keypad_name(self):
        """Returns the name of the keypad which contains this button."""
        return self._keypad.name

    @property
    def keypad_vid(self):
        """Returns the VID of the keypad which contains this button."""
        return self._parent

    def handle_update(self, args, _):
        """The callback invoked by the main event loop.

        This callback is invoked from the VantageConnection thread.

        """
        action = args[0]
        _LOGGER.debug("Button %d(%s): action=%s params=%s",
                      self._vid, self._name, action, args[1:])
        if self._keypad:  # it's a button
            self._value = action
            # this transfers control to Keypad.handle_update(...)
            self._vantage.handle_update_and_notify(
                self._keypad, [self._num, self._name, self._value],
                self._vid)
        else:  # it's a drycontact
            # TODO: support per-vid flipping/control of these rewrites
            if action == 'PRESS':
                self._value = 'Violated'
            elif action == 'RELEASE':
                self._value = 'Normal'
            else:
                _LOGGER.warning(
                    "unexpected action for drycontact button %s = %s",
                    self, action)
                self._value = action

        return self


class LoadGroup(Output):
    """Represent a Vantage LoadGroup."""
    def __init__(self, vantage, name, area, load_vids, color_vids,
                 dmx_color, support_color_temp, vid):
        """Initialize a load group"""
        super(LoadGroup, self).__init__(
            vantage, name, area, 'GROUP', 'GROUP', None, dmx_color, vid)
        self._load_vids = load_vids
        self._color_vids = color_vids
        self._support_color_temp = support_color_temp
        self._brightness_vid = None
        if len(self._load_vids) == 2 and len(self._color_vids) == 1:
            if self._load_vids[0] == self._color_vids[0]:
                self._brightness_vid = self._load_vids[1]
            else:
                self._brightness_vid = self._load_vids[0]
        if self._brightness_vid:
            self._vantage._brightnessvid_to_group_vid[
                self._brightness_vid] = self._vid

        for v in load_vids:
            if self._vantage._vid_to_load[v]._is_dimmable:
                self._is_dimmable = True
                break

    def support_color_temp(self):
        """Returns true iff this load can be set to a color temperature."""
        return self._support_color_temp

    def __str__(self):
        """Returns a pretty-printed string for this object."""
        return ("Output name: '%s' area: %d type: '%s' load: '%s' "
                "id: %d %s%s%s%s (%s) (c:%s) (b:%s) [%s]" % (
                    self._name, self._area, self._output_type,
                    self._load_type, self._vid,
                    ("(dim) " if self.is_dimmable else ""),
                    ("(ctemp) " if self.support_color_temp else ""),
                    ("(color) " if self.support_color else ""),
                    ("(dirty) " if self._rgb_is_dirty else ""),
                    self._load_vids,
                    self._color_vids,
                    self._brightness_vid,
                    self.full_lineage))

    def last_level(self):
        if self._brightness_vid:
            return self._vantage._vid_to_load.get(self._brightness_vid)._level
        else:
            return self._level

    def _get_level(self):
        """Returns the output level of the group.
        Iff there is one non-color and one color load, then delegate to the non-color load."""
        if self._brightness_vid:
            return self._vantage._vid_to_load.get(self._brightness_vid).level
        else:
            return super(LoadGroup, self)._get_level()

    def _set_level(self, new_level):
        if self._brightness_vid:
            self._vantage._vid_to_load.get(self._brightness_vid).level = new_level
        super(LoadGroup, self)._set_level(new_level)

    level = property(_get_level, _set_level)

    # Load Groups do not respond to RGBLoad.SetRGBW invocations
    # so we need to call them for each of the member groups that do
    def _invoke_rgb(self):
        """Update the RGB of the load group to self._rgb"""
        (r, g, b) = self._rgb
        ratio = self._level/100
        for vid in self._load_vids:
            load = self._vantage._vid_to_load.get(vid)
            if load and (load._dmx_color or load._load_type == "DW"):
                self._vantage.send("INVOKE", vid,
                                   ("RGBLoad.SetRGBW %d %d %d %d" %
                                    (r*ratio, g*ratio, b*ratio, 0)))
        if self._level > 0:
            self._rgb_is_dirty = False

    def _invoke_hs(self):
        """Update the RGB of the load group to self._rgb"""
        (h, s) = self._hs
        for vid in self._load_vids:
            load = self._vantage._vid_to_load.get(vid)
            if load and (load._dmx_color or load._load_type == "DW"):
                self._vantage.send("INVOKE", vid,
                                   ("RGBLoad.SetHSL %d %d %d" %
                                    (h, s, self._level)))

    def __do_query_level(self):
        """Helper to perform the actual query the current dimmer level of the
        output. For pure on/off loads the result is either 0.0 or 100.0."""
        if self.support_color and not self._addedstatus:
            _LOGGER.debug("Using first color_vid = %s to ADDSTATUS for %s",
                          self._color_vids[0], self._vid)
            # it appears to be ok to have ADDSTATUS called multiple times on
            # the same vid and it only counts 1 towards the 64 limit per
            # connection
            self._vantage._colorvid_to_group_vid[
                self._color_vids[0]] = self._vid
            self._vantage.send("ADDSTATUS", self._color_vids[0])
            self._addedstatus = True
        _LOGGER.debug("getload of %s", self._vid)
        self._vantage.send("GETLOAD", self._vid)


class Keypad(VantageSensor):
    """Object representing a Vantage keypad.

    Currently we don't really do much with it except handle the events
    (and drop them on the floor).
    """
    CMD_TYPE = 'KEYPAD'  # for a keypad

    def __init__(self, vantage, name, area, vid):
        """Initializes the Keypad object."""
        super(Keypad, self).__init__(vantage, name, area, vid)
        self._buttons = []
        self._vantage.register_id(Keypad.CMD_TYPE, None, self)

    def add_button(self, button):
        """Adds a button that's part of this keypad. We'll use this to
        dispatch button events."""
        self._buttons.append(button)

    def __str__(self):
        """Returns a pretty-printed string for this object."""
        return 'Keypad name: "%s", area: "%s", vid: %d' % (
            self._name, self._area, self._vid)

    @property
    def buttons(self):
        """Return a tuple of buttons for this keypad."""
        return tuple(button for button in self._buttons)

    @property
    def kind(self):
        """The type of object (for units in hass)."""
        return 'keypad'

    def handle_update(self, args, _):
        """The callback invoked by a button's handle_update to
        set keypad value to the name of button.

        This callback is invoked from the VantageConnection thread.

        """
        _LOGGER.debug("Keypad %d(%s): %s",
                      self._vid, self._name, args)
        self._value = args[0]
        self._extra_info['button_name'] = args[1]
        self._extra_info['button_action'] = args[2]
        return self


class Task(VantageEntity):
    """Object representing a Vantage task.

    """
    CMD_TYPE = 'TASK'

    def __init__(self, vantage, name, vid):
        """Initializes the Task object."""
        super(Task, self).__init__(vantage, name, 0, vid)
        self._vantage.register_id(Task.CMD_TYPE, None, self)

    def __str__(self):
        """Returns a pretty-printed string for this object."""
        return 'Task name: "%s", vid: %d' % (
            self._name, self._vid)

    def handle_update(self, args, _):
        """Handle events from the task object.

        This callback is invoked from the VantageConnection thread.

        """
        component = int(args[0])
        action = int(args[1])
        params = [int(x) for x in args[2:]]
        _LOGGER.debug("Task %d(%s): c=%d a=%d params=%s",
                      self._vid, self._name, component, action, params)
        return self


class PollingSensor(VantageSensor):
    """Base class for LightSensor and OmniSensor.
    These sensors do not report values via STATUS commands
    but instead need to be polled."""

    def __init__(self, vantage, name, area, vid, kind):
        """Init base fields"""
        assert name is not None
        super(PollingSensor, self).__init__(vantage, name, area, vid)
        self._kind = kind

    def needs_poll(self):
        return True

    @property
    def kind(self):
        """The type of object (for units in hass)."""
        return self._kind

    def update(self):
        """Request an update from the device."""
        k = self._kind.upper()
        if k == 'LIGHTSENSOR':
            k = 'LIGHT'
        elif k.startswith('VARIABLE'):
            k = 'VARIABLE'
        self._vantage.send("GET"+k, self._vid)

    def handle_update(self, args, _):
        """Handle sensor updates.

        This callback is invoked from the VantageConnection thread.

        """

        try:
            if self._kind == 'variable_text':
                # "he said ""she said"" then left" =>
                #     he said "she said" then left
                # i.e., remove leading and trailing quotes
                # and undouble internal quotes
                value = args[0][1:-1].replace('""', '"')
            elif self._kind == 'variable_bool':
                value = args[0] == '1'
            else:
                value = float(args[0])
        except Exception:
            value = args[0]
        _LOGGER.debug("Setting sensor (%s) %s (%d) to %s",
                      self._name, self._kind, self._vid, value)
        self._value = value
        return self


class Variable(PollingSensor):
    """A variable in the vantage system. See set_variable_vid.

    """
    CMD_TYPE = 'VARIABLE'  # GMem in the XML config

    def __init__(self, vantage, name, vid, subtype):
        """Initializes the variable object."""
        super(Variable, self).__init__(vantage, name, None, vid,
                                       'variable' + "_" + subtype)
        self._vantage.register_id(Variable.CMD_TYPE, None, self)

    def __str__(self):
        """Returns pretty-printed representation of this object."""
        return 'Variable name: "%s", vid: %d, value: %s' % (
            self._name, self._vid, self._value)

    @property
    def value(self):
        return super(Variable, self).value

    @value.setter
    def value(self, val):
        """Sets the variable to val. """
        self._value = val
        if self._kind == 'variable_text':
            val = '"' + val.replace('"', '""') + '"'
        elif self._kind == 'variable_bool':
            val = 1 if val else 0
        self._vantage.send("VARIABLE", self._vid, val)


class LightSensor(PollingSensor):
    """Represent LightSensor devices."""
    CMD_TYPE = 'LIGHT'

    def __init__(self, vantage, name, area, value_range, vid):
        """Initializes the motion sensor object."""
        assert name is not None
        super(LightSensor, self).__init__(vantage, name,
                                          area, vid,
                                          'lightsensor')
        self.value_range = value_range
        self._vantage.register_id(self.CMD_TYPE, None, self)

    def __str__(self):
        """Returns pretty-printed representation of this object."""
        return ('LightSensor name (%s), area: "%s", '
                '"kind: "%s", vid: %d, value: %s' % (
                    self._name, self._area, self._kind,
                    self._vid, self._value))


class OmniSensor(PollingSensor):
    """An omnisensor in the vantage system."""
    CMD_TYPE = 'SENSOR'  # OmniSensor in the XML config

    def __init__(self, vantage, name, kind, vid):
        """Initializes the sensor object."""
        super(OmniSensor, self).__init__(vantage, name, None, vid,
                                         kind)
        self._vantage.register_id(self._kind.upper(), None, self)

    def __str__(self):
        """Returns pretty-printed representation of this object."""
        return 'OmniSensor name (%s): "%s", vid: %d, value: %s' % (
            self._name, self._kind, self._vid, self._value)


class Shade(VantageEntity):
    """A shade in the vantage system.

    """
    CMD_TYPE = 'BLIND'  # MechoShade.IQ2_Shade_Node_CHILD in the XML config
    _wait_seconds = 0.03  # TODO:move this to a parameter

    def __init__(self, vantage, name, area_vid, vid):
        """Initializes the shade object."""
        super(Shade, self).__init__(vantage, name, area_vid, vid)
        self._level = 100
        self._load_type = 'BLIND'
        self._extra_info['load_type'] = self._load_type
        self._vantage.register_id(Shade.CMD_TYPE, None, self)
        self._query_waiters = _RequestHelper()

    def __str__(self):
        """Returns pretty-printed representation of this object."""
        return 'Shade name: "%s", vid: %d, area: %d, level: %s' % (
            self._name, self._vid, self._area, self._level)

    def __repr__(self):
        """Returns a stringified representation of this object."""
        return str({'name': self._name, 'area': self._area,
                    'type': self._load_type, 'vid': self._vid})

    def last_level(self):
        """Returns last cached value of output level, no query is performed."""
        return self._level

    @property
    def level(self):
        """The level (i.e. position) of the shade.
        Returns the current output level by querying the remote controller."""
        ev = self._query_waiters.request(self.__do_query_level)
        ev.wait(self._wait_seconds)
        return self._level

    @level.setter
    def level(self, new_level):
        """Sets the new output level."""
        if self._level == new_level:
            return
        if new_level == 0:
            self.close()
        elif new_level == 100:
            self.open()
        else:
            if new_level is not None:
                self._vantage.send("BLIND", self._vid, "POS", str(new_level))
        self._level = new_level

    def __do_query_level(self):
        """Helper to fetch the current [possibly inferred] shade level
        as a percentage of open. 100 = fully open."""
        self._vantage.send("GETBLIND", self._vid)

    def open(self):
        """Open the shade."""
        self._vantage.send("BLIND", self._vid, "OPEN")

    def stop(self):
        """Stop the shade."""
        self._vantage.send("BLIND", self._vid, "STOP")

    def close(self):
        """Stop the shade."""
        self._vantage.send("BLIND", self._vid, "CLOSE")

    def handle_update(self, args, _):
        """Handle new value for shade.

        This callback is invoked from the VantageConnection thread.

        """
        value = args[0]
        if value == "OPEN":
            value = 100.0
        elif value == "CLOSE":
            value = 0.0
        elif value == "STOP":
            value = None
        elif value == "POS":
            value = float(args[1])
        else:
            value = float(value)
        _LOGGER.debug("Setting shade %s (%d) to float %s",
                      self._name, self._vid, str(value))
        self._level = value
        return self
