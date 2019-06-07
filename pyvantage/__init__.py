"""
Vantage Controller module for interacting with the infusion controller via TCP.
Basic operations for enumerating and controlling the loads are supported.

Author: Greg J. Badros

Based on pylutron which was written by Dima Zavin

See also https://www.npmjs.com/package/vantage-infusion
and https://github.com/angeloxx/homebridge-vantage/blob/master/index.js

To use with home assistant and its virtual python environment, you need to:

$ cd .../path/to/home-assistant/
$ pip3 install --upgrade .../path/to/pyvantage

Then the component/vantage.py and its require line will work.

"""

__Author__ = "Greg J. Badros"
__copyright__ = "Copyright 2018, Greg J. Badros"
  # Dima Zavin wrote pylutron on which this is heavily based

import logging
import telnetlib
import socket
import threading
import time
import base64
import re
import json
from colormath.color_objects import LabColor, xyYColor, sRGBColor, HSVColor
from colormath.color_conversions import convert_color


def xml_escape(s):
    """Escape XML meta characters '<' and '&'."""
    answer = s.replace("<", "&lt;")
    answer = answer.replace("&", "&amp;")
    return answer

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
    pass


class VIDExistsError(VantageException):
    """Asserted when there's an attempt to register a duplicate integration id."""
    pass


class ConnectionExistsError(VantageException):
    """Raised when a connection already exists (e.g. user calls connect() twice)."""
    pass


class VantageConnection(threading.Thread):
    """Encapsulates the connection to the Vantage controller."""

    def __init__(self, host, user, password, cmd_port, recv_callback):
        """Initializes the vantage connection, doesn't actually connect."""
        threading.Thread.__init__(self)

        self._host = host
        self._user = user
        self._password = password
        self._cmd_port = cmd_port
        self._telnet = None
        self._connected = False
        self._lock = threading.Lock()
        self._connect_cond = threading.Condition(lock=self._lock)
        self._recv_cb = recv_callback
        self._done = False

        self.setDaemon(True)

    def connect(self):
        """Connects to the vantage controller."""
        if self._connected or self.is_alive():
            raise ConnectionExistsError("Already connected")
        # After starting the thread we wait for it to post us
        # an event signifying that connection is established. This
        # ensures that the caller only resumes when we are fully connected.
        self.start()
        with self._lock:
            self._connect_cond.wait_for(lambda: self._connected)

    # VantageConnection
    def send_ascii_nl(self, cmd):
        """Sends the specified command to the vantage controller."""
        _LOGGER.info("Vantage send_ascii_nl: %s", cmd)
        try:
            self._telnet.write(cmd.encode('ascii') + b'\r\n')
        except BrokenPipeError:
            _LOGGER.warning("Vantage BrokenPipeError - disconnected")
            self._disconnect()

    def _do_login(self):
        """Executes the login procedure (telnet) as well as setting up some
        connection defaults like turning off the prompt, etc."""
        self._telnet = telnetlib.Telnet(self._host, self._cmd_port)
        self.send_ascii_nl("LOGIN " + self._user + " " + self._password)
        self._telnet.read_until(b'\r\n')
        self.send_ascii_nl("STATUS load")
        self._telnet.read_until(b'\r\n')
        self.send_ascii_nl("STATUS blind")
        self._telnet.read_until(b'\r\n')
        self.send_ascii_nl("STATUS led")
        self._telnet.read_until(b'\r\n')
        self.send_ascii_nl("STATUS variable")
        return True

    def _disconnect(self):
        with self._lock:
            self._connected = False
            self._connect_cond.notify_all()
            self._telnet = None
            _LOGGER.warning("Disconnected")

    def _maybe_reconnect(self):
        """Reconnects to the controller if we have been previously disconnected."""
        with self._lock:
            if not self._connected:
                _LOGGER.info("Connecting")
                self._lock.release()
                try:
                    self._do_login()
                finally:
                    self._lock.acquire()
                self._connected = True
                self._connect_cond.notify_all()
                _LOGGER.info("Connected")

    def run(self):
        """Main thread function to maintain connection and receive remote status."""
        _LOGGER.info("Started")
        while True:
            self._maybe_reconnect()
            try:
                line = self._telnet.read_until(b"\n")
            except EOFError:
                _LOGGER.warning("run got EOFError")
                self._disconnect()
                continue
            self._recv_cb(line.decode('ascii').rstrip())
            _LOGGER.debug("finished _recv_cb")


class VantageXmlDbParser():
    """The parser for Vantage XML database.

    The database describes all the rooms (Area), keypads (Device), and switches
    (Output). We handle the most relevant features, but some things like LEDs,
    etc. are not implemented."""

    def __init__(self, vantage, xml_db_str):
        """Initializes the XML parser, takes the raw XML data as string input."""
        self._vantage = vantage
        self._xml_db_str = xml_db_str
        self.outputs = []
        self.variables = []
        self.tasks = []
        self.load_groups = []
        self.vid_to_area = {}
        self.vid_to_load = {}
        self.vid_to_keypad = {}
        self.vid_to_button = {}
        self.vid_to_variable = {}
        self.vid_to_task = {}
        self.name_to_task = {}
        self.vid_to_shade = {}
        self._name_area_to_vid = {}
        self._vid_to_colorvid = {}
        self.project_name = None

    def parse(self):
        """Main entrypoint into the parser. It interprets and creates all the
        relevant Vantage objects and stuffs them into the appropriate hierarchy."""
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
                _LOGGER.info("Set project name to %s", self.project_name)
            area = self._parse_area(area_xml)
            _LOGGER.info("Area = %s", area)
            self.vid_to_area[area.vid] = area

        loads = root.findall(".//Objects//Load[@VID]")
        loads = loads + root.findall(".//Objects//Vantage.DDGColorLoad[@VID]")
        for load_xml in loads:
            output = self._parse_output(load_xml)
            if output is None:
                continue
            self.outputs.append(output)
            self.vid_to_load[output.vid] = output
            _LOGGER.info("Output = %s", output)
            self.vid_to_area[output.area].add_output(output)

        load_groups = root.findall(".//Objects//LoadGroup[@VID]")
        for lg_xml in load_groups:
            lgroup = self._parse_load_group(lg_xml)
            if lgroup is None:
                continue
            self.load_groups.append(lgroup)
            self.outputs.append(lgroup)
            self.vid_to_load[lgroup.vid] = lgroup
            _LOGGER.info("load group = %s", lgroup)
            self.vid_to_area[lgroup.area].add_output(lgroup)

        keypads = root.findall(".//Objects//Keypad[@VID]")
        for kp_xml in keypads:
            keypad = self._parse_keypad(kp_xml)
            self.vid_to_keypad[keypad.vid] = keypad
            _LOGGER.info("keypad = %s", keypad)
            self.vid_to_area[keypad.area].add_keypad(keypad)

        buttons = root.findall(".//Objects//Button[@VID]")
        for button_xml in buttons:
            b = self._parse_button(button_xml)
            _LOGGER.info("b = %s", b)

        variables = root.findall(".//Objects//GMem[@VID]")
        for v in variables:
            var = self._parse_variable(v)
            self.vid_to_variable[var.vid] = var
            self.variables.append(var)
            _LOGGER.info("var = %s", var)

        tasks = root.findall(".//Objects//Task[@VID]")
        for t in tasks:
            task = self._parse_task(t)
            self.vid_to_task[task.vid] = task
            self.name_to_task[task.name] = task
            self.tasks.append(task)
            _LOGGER.info("task = %s", task)

        ## Lots of different shade types, one xpath for each kind of shade
        # MechoShade driver shades
        shades = root.findall(".//Objects//MechoShade.IQ2_Shade_Node_CHILD[@VID]")
        shades = shades + root.findall(".//Objects//MechoShade.IQ2_Group_CHILD[@VID]")
        # Native QIS QMotion shades
        shades = shades + root.findall(".//Objects//QISBlind[@VID]")
        shades = shades + root.findall(".//Objects//BlindGroup[@VID]")
        # Non-native QIS Driver QMotion shades (the old way)
        shades = shades + root.findall(".//Objects//QMotion.QIS_Channel_CHILD[@VID]")
        # Somfy radio-controlled
        shades = shades + root.findall(".//Objects//Somfy.URTSI_2_Shade_CHILD[@VID]")
        # Somfy RS-485 SDN wired shades
        shades = shades + root.findall(".//Objects//Somfy.RS-485_Shade_CHILD[@VID]")

        for shade_xml in shades:
            shade = self._parse_shade(shade_xml)
            if shade is None:
                continue
            self.vid_to_shade[shade.vid] = shade
            self.outputs.append(shade)
            _LOGGER.info("shade = %s", shade)

        _LOGGER.info("self._name_area_to_vid = %s", self._name_area_to_vid)

        return True

    def _parse_area(self, area_xml):
        """Parses an Area tag, which is effectively a room, depending on how the
        Vantage controller programming was done."""
        area = Area(self._vantage,
                    name=area_xml.find('Name').text,
                    parent=int(area_xml.find('Area').text),
                    vid=int(area_xml.get('VID')),
                    note=area_xml.find('Note').text)
        return area

    def _parse_variable(self, var_xml):
        """Parses a variable tag."""
        var = Variable(self._vantage,
                       name=var_xml.find('Name').text,
                       vid=int(var_xml.get('VID')))
        return var

    def _parse_shade(self, shade_xml):
        """Parses a MechoShade.IQ2_Shade_Node_CHILD or QMotion.QIS_Channel_CHILD (shade) tag."""
        shade = Shade(self._vantage,
                      name=shade_xml.find('Name').text,
                      area_vid=int(shade_xml.find('Area').text),
                      vid=int(shade_xml.get('VID')))
        return shade

    def _parse_output(self, output_xml):
        """Parses a load, which is generally a switch controlling a set of
        lights/outlets, etc."""
        out_name = output_xml.find('DName').text
        if out_name:
            out_name = out_name.strip()
        if not out_name or out_name.isspace():
            out_name = output_xml.find('Name').text.strip()
        area_vid = int(output_xml.find('Area').text)

        area_name = self.vid_to_area[area_vid].name.strip()
        lt_xml = output_xml.find('LoadType')
        if lt_xml is not None:
            load_type = lt_xml.text.strip()
        else:
            load_type = output_xml.find('ColorType').text.strip()

        output_type = 'LIGHT'
        vid = int(output_xml.get('VID'))

        # TODO: find a better heuristic so that on/off lights still show up
        if load_type in ('High Voltage Relay', 'Low Voltage Relay'):
            output_type = 'RELAY'
        if out_name == "NOT USED":
            return None
        # a RELAY load and a DIMLOAD load are software-paired together and
        # a LoadGroup containing both should exist in vantage if it's supposed
        # to show up in the Outputs available here (and in home assistant)
        if out_name.endswith('DIMLOAD'):
            output_type = 'SKIP ' + output_type
        if out_name.startswith('Station Load '):
            return None
        if out_name.startswith('Color Load '):
            return None

        is_relay_area = False
        if area_name.strip() == '0-10V RELAYS':
            _LOGGER.info("Skipping %s because in 0-10V RELAYS area",
                         out_name)
            is_relay_area = True
            output_type = 'SKIP ' + output_type

        if ' COLOR' in out_name and load_type != 'HID':
            _LOGGER.warning("Load %s [%d] might be color load but of type %s not HID",
                            out_name, vid, load_type)

        if load_type == 'HID':
            output_type = 'COLOR'
            omit_trailing_color_re = re.compile(r'\s+COLOR\s*$')
            load_name = omit_trailing_color_re.sub("", out_name)
            _LOGGER.info("Found HID Type, guessing load name is %s", load_name)

            load_vid = self._name_area_to_vid.get((load_name, area_vid), None)
            if load_vid:
                self._vid_to_colorvid[load_vid] = vid
                _LOGGER.info("Found colorvid = %d for load_vid %d (names %s and %s)"
                             " in area %s (%d)",
                             vid, load_vid, out_name, load_name, area_name, area_vid)
                self.vid_to_load[load_vid].color_control_vid = vid
            else:
                # TODO: do not assume that the regular loads are handled before the COLOR loads
                _LOGGER.warning("Could not find matching load for COLOR load %s (%d)"
                                " in area %s (%d)",
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
            # _LOGGER.info("ch1 = %s, ch2 = %s", ch1.text, ch2.text)
            if not(ch1.text and ch1.text.strip() != ""):
                _LOGGER.warning("RGB* load with missing Channel1: %s", out_name)
            if not(ch3.text and ch3.text.strip() != ""):
                _LOGGER.warning("RGB* load with missing Channel3: %s", out_name)
            if load_type == "RGBW":
                if not(ch2.text and ch2.text.strip() != ""):
                    _LOGGER.warning("RGBW load with missing Channel2: %s", out_name)
                dmx_color = True
            else: # load_type == "RGB"
                if ch2.text and ch2.text.strip() != "":
                    dmx_color = True
                else:
                    # just a dynamic white red/blue light (just two shades of white, really)
                    load_type = "DW"

        if output_type == 'LIGHT':
            self._name_area_to_vid[(out_name, area_vid)] = vid
# TODO: use this?
#    if is_relay_area:
#      return None
        output = Output(self._vantage,
                        name=out_name,
                        area=area_vid,
                        output_type=output_type,
                        load_type=load_type,
                        cc_vid=(load_vid if output_type == 'COLOR'
                                else self._vid_to_colorvid.get(vid, None)),
                        dmx_color=dmx_color,
                        vid=vid)
        return output

    def _parse_load_group(self, output_xml):
        """Parses a load group, which is a set of loads"""
        out_name = output_xml.find('DName').text
        if out_name:
            out_name = out_name.strip()
        if not out_name or out_name.isspace():
            out_name = output_xml.find('Name').text
        else:
            _LOGGER.info("Using dname = %s", out_name)
        area_vid = int(output_xml.find('Area').text)

#        area_name = self.vid_to_area[area_vid].name
        loads = output_xml.findall('./LoadTable/Load')
        vid = output_xml.get('VID')
        vid = int(vid)

        load_vids = []
        dmx_color = True
        for load in loads:
            v = int(load.text)
            load_vids.append(v)
            if not self.vid_to_load[v]._dmx_color:
                dmx_color = False
            else:
                _LOGGER.warning("for loadgroup %d, vid %s supports color", vid, v)


        output = LoadGroup(self._vantage,
                           name=out_name,
                           area=area_vid,
                           load_vids=load_vids,
                           dmx_color=dmx_color,
                           vid=vid)
        return output

    def _parse_keypad(self, keypad_xml):
        """Parses a keypad device."""
        area_vid = int(keypad_xml.find('Area').text)
        keypad = Keypad(self._vantage,
                        name=keypad_xml.find('Name').text,
                        area=area_vid,
                        vid=int(keypad_xml.get('VID')))
        return keypad

    def _parse_task(self, task_xml):
        """Parses a task object."""
        task = Task(self._vantage,
                    name=task_xml.find('Name').text,
                    vid=int(task_xml.get('VID')))
        return task

    def _parse_button(self, button_xml):
        """Parses a button device that part of a keypad."""
        vid = int(button_xml.get('VID'))
        name = button_xml.find('Name').text
        parent = button_xml.find('Parent')
        parent_vid = int(parent.text)
        num = int(parent.get('Position'))
        keypad = self._vantage._ids['KEYPAD'].get(parent_vid, None)
        if keypad is None:
            _LOGGER.warning("Could not find parent vid = %d for button vid = %d", parent_vid, vid)
            area = -1
        else:
            area = keypad.area
        button = Button(self._vantage, name, area, vid, num, parent_vid)
        if keypad:
            keypad.add_button(button)
        return button

    def _parse_motion_sensor(self, sensor_xml):
        """Parses a motion sensor object.

        TODO: We don't actually do anything with these yet. There's a lot of info
        that needs to be managed to do this right. We'd have to manage the occupancy
        groups, what's assigned to them, and when they go (un)occupied. We'll handle
        this later.
        """
        return MotionSensor(self._vantage,
                            name=sensor_xml.get('Name'),
                            area=None,
                            vid=int(sensor_xml.get('VID')))

# Connect to port 2001 and write
# "<IBackup><GetFile><call>Backup\\Project.dc</call></GetFile></IBackup>"
# to get a Base64 response of the last XML file of the designcenter config.
# Then use port 3001 to send commands.

# maybe need <ILogin><Login><call><User>USER</User><Password>PASS</Password></call></Login></ILogin>


class Vantage():
    """Main Vantage Controller class.

    This object owns the connection to the controller, the rooms that exist in the
    network, handles dispatch of incoming status updates, etc.
    """

    # See vantage host commands reference (you may need to be a dealer/integrator for access)
    OP_RESPONSE = 'R:'        # Response lines come back from Vantage with this prefix
    OP_STATUS = 'S:'          # Status report lines come back from Vantage with this prefix

    def __init__(self, host, user, password,
                 only_areas=None, exclude_areas=None,
                 cmd_port=3001, file_port=2001,
                 name_mappings=None):
        """Initializes the Vantage object. No connection is made to the remote
        device."""
        self._host = host
        self._user = user
        self._password = password
        self._name = None
        self._conn = VantageConnection(host, user, password, cmd_port, self._recv)
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
        self._r_cmds = ['LOGIN', 'LOAD', 'STATUS', 'GETLOAD', 'VARIABLE', 'TASK',
                        'GETBLIND', 'BLIND', 'INVOKE']
        self._s_cmds = ['LOAD', 'TASK', 'LED', 'VARIABLE', 'BLIND', 'STATUS']

    def subscribe(self, obj, handler):
        """Subscribes to status updates of the requested object.

        The handler will be invoked when the controller sends a notification
        regarding changed state. The user can then further query the object for the
        state itself."""
        self._subscribers[obj] = handler

    def get_lineage_from_obj(self, obj):
        """Return list of areas for obj, chasing up to top."""
        count = 0
        area = self._vid_to_area.get(obj.area, None)
        if area is None:
            return []
        answer = [area.name]
        while area and count < 10:
            count += 1
            parent_vid = area.parent
            if parent_vid == 0:
                break
            area = self._vid_to_area.get(parent_vid, None)
            if area:
                answer.append(area.name)
#    _LOGGER.info("lineage for " + str(obj.vid) + " is " + str(answer))
        return answer

    #TODO: cleanup this awful logic
    def register_id(self, cmd_type, cmd_type2, obj):
        """Registers an object (through its vid [vantage id]) to receive update
        notifications. This is the core mechanism how Output and Keypad objects get
        notified when the controller sends status updates."""
        ids = self._ids.setdefault(cmd_type, {})
        ids = self._ids.setdefault(cmd_type2, {})
        if obj.vid in ids:
            raise VIDExistsError("VID exists %s" % obj.vid)
        self._ids[cmd_type][obj.vid] = obj
        if cmd_type2:
            self._ids[cmd_type2][obj.vid] = obj
        lineage = self.get_lineage_from_obj(obj)
        name = ""
        for n in reversed(lineage[:-1]):  # reverse all but the last element in list
            ns = n.strip()
            if ns.startswith('Station Load '):
                continue
            if ns.startswith('Color Load '):
                continue
            if self._name_mappings:
                mapped_name = self._name_mappings.get(ns.lower(), None)
                if mapped_name is not None:
                    if mapped_name is True:
                        continue
                    ns = mapped_name
            name += ns + "-"

        # TODO: this may be a little too hacky
        # it makes sure that we use "GH-Bedroom High East"
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
            if '0-10V RELAYS' in oldname or cmd_type == 'LED':
                _LOGGER.info("Repeated name `%s' - adding vid to get %s", oldname, obj.name)
            else:
                _LOGGER.warning("Repeated name `%s' - adding vid to get %s", oldname, obj.name)
        self._names[obj.name] = obj.vid


    # TODO: update this to handle async status updates
    def _recv(self, line):
        """Invoked by the connection manager to process incoming data."""
        _LOGGER.debug("_recv got line: %s", line)
        if line == '':
            return
        typ = None
        # Only handle query response messages, which are also sent on remote status
        # updates (e.g. user manually pressed a keypad button)
#    if line.find(Vantage.OP_RESPONSE) != 0:
#      _LOGGER.debug("ignoring %s" % line)
#      return
        if line[0] == 'R':
            cmds = self._r_cmds
            typ = 'R'
        elif line[0] == 'S':
            cmds = self._s_cmds
            typ = 'S'
        else:
            _LOGGER.error("_recv got unknown line start character")
            return
        parts = re.split(r'[ :]', line[2:])
        cmd_type = parts[0]
        vid = parts[1]
        args = parts[2:]
        if cmd_type not in cmds:
            _LOGGER.warning("Unknown cmd %s (%s)", cmd_type, line)
            return
        if cmd_type == 'ERROR':
            _LOGGER.error("_recv got ERROR line: %s", line)
            return
        if cmd_type == 'LOGIN':
            _LOGGER.info("login successful")
            return
        if line[0] == 'R' and cmd_type in ('STATUS', 'INVOKE'):
            return
        if cmd_type == 'GETLOAD':
            cmd_type = 'LOAD'
        elif cmd_type == 'GETBLIND':
            return
        elif cmd_type == 'TASK':
            return
        elif cmd_type == 'VARIABLE':
            _LOGGER.info("vantage variable set response: %s", line)

        ids = self._ids.get(cmd_type, None)
        if ids is None:
            _LOGGER.warning("Might need to handle cmd_type ids: %s:: %s", cmd_type, line)
        else:
            if not vid.isdigit():
                _LOGGER.warning("VID %s is not an integer", vid)
                return
            vid = int(vid)
            if vid not in ids:
#        if not line.startswith("S:LED"):
                _LOGGER.warning("Unknown id %d (%s)", vid, line)
                return
            obj = ids[vid]
            # First let the device update itself
            if typ == 'S' or (typ == 'R' and cmd_type == 'LOAD'):
                handled = obj.handle_update(args)
                # Now notify anyone who cares that device  may have changed
                if handled and handled in self._subscribers:
                    self._subscribers[handled](handled)

    def connect(self):
        """Connects to the Vantage controller to send and receive commands and status"""
        self._conn.connect()

    # Vantage
    def send_cmd(self, cmd):
        """Send the host command to the Vantage TCP socket."""
        self._conn.send_ascii_nl(cmd)

    # Vantage
    def send(self, op, vid, *args):
        """Formats and sends the requested command to the Vantage controller."""
#    out_cmd = ",".join(
#        (cmd, str(vid)) + tuple((str(x) for x in args)))
        out_cmd = str(vid) + " " + " ".join(args)
        self._conn.send_ascii_nl(op + " " + out_cmd)

    # TODO: could confirm that this variable exists in the XML we download
    # and/or lookup the variables VID so that we can set it by name
    def set_variable_vid(self, vid, value):
        """Sets variable with vid to value;
        be sure instance type of value is either int or string"""
        num = re.compile(r'^\d+$')
        if isinstance(value, int) or num.match(value):
            self._conn.send_ascii_nl("VARIABLE " + str(vid) + " " + str(value))
        else:
            p = re.compile(r'["\n\r]')
            if p.match(value):
                raise Exception("Newlines and quotes are not allowed in Text values")
            self._conn.send_ascii_nl("VARIABLE " + str(vid) + ' "' + value + '"')

    def call_task_vid(self, vid):
        """Call the task with vid."""
        num = re.compile(r'^\d+$')
        if isinstance(vid, int) or num.match(vid):
            task = self._vid_to_task.get(int(vid), None)
            if task is None:
                _LOGGER.warning("Vid %d is not registered as a task", vid)
            # call it regardless
            self._conn.send_ascii_nl("TASK " + str(vid) + " RELEASE")
            _LOGGER.info("Calling task %s", task)
        else:
            _LOGGER.warning("Could not interpret %d as task vid", vid)

    def call_task(self, name):
        """Call the task with name NAME (fragile - consider using call_task_vid)."""
        task = self._name_to_task.get(name, None)
        if task is not None:
            self._conn.send_ascii_nl("TASK " + str(task.vid) + " RELEASE")
            _LOGGER.info("Calling task %s", task)
        else:
            _LOGGER.warning("No task with name = %s", name)

    def load_xml_db(self, disable_cache=False):
        """Load the Vantage database from the server."""
        filename = self._host + "_config.txt"
        xml_db = ""
        success = False
        if not disable_cache:
            try:
                f = open(filename, "r")
                xml_db = f.read()
                f.close()
                success = True
                _LOGGER.info("read cached vantage configuration file %s", filename)
            except Exception as e:
                _LOGGER.warning("Failed loading cached config file for vantage: %s", e)
        if not success:
            _LOGGER.info("doing request for vantage configuration file")
            if disable_cache:
                _LOGGER.info("Vantage config cache is disabled.")
            ts = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ts.connect((self._host, self._file_port))
            if self._user:
                ts.send(("<ILogin><Login><call><User>%s</User><Password>%s</Password>"
                         "</call></Login></ILogin>\n"
                         % (xml_escape(self._user),
                            xml_escape(self._password))).encode("ascii"))
                response = ""
                while not response.endswith("</ILogin>\n"):
                    response += ts.recv(4096).decode('ascii')
                check_return_true = re.compile(r'<return>(.*?)</return>')
                m = check_return_true.search(response)
                if m is None:
                    raise Exception("Could not find response code from controller "
                                    "upon login attempt, response = "  + response)
                if m.group(1) != "true":
                    raise Exception("Login failed, return code is: " + m.group(1))
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
            _LOGGER.info("done reading")
            response = response.decode('ascii')
            response = response[response.find("</Result>\n")+10:]
            response = response.replace('<?File Encode="Base64" /', '')
            response = response.replace('?>', '')
            response = response[:response.find('</return>')]
            dbytes = base64.b64decode(response)
            xml_db = dbytes.decode('utf-8')
            try:
                f = open(filename, "w")
                f.write(xml_db)
                f.close()
                _LOGGER.info("wrote file %s", filename)
            except:
                _LOGGER.warning("could not save %s", filename)

        _LOGGER.info("Loaded xml db")
        # print(xml_db[0:10000])

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
        self._vid_to_load = parser.vid_to_load
        self._vid_to_variable = parser.vid_to_variable
        self._vid_to_area = parser.vid_to_area
        self._vid_to_shade = parser.vid_to_shade
        self._vid_to_task = parser.vid_to_task
        self._name_to_task = parser.name_to_task
        self._name = parser.project_name

        _LOGGER.info('Found Vantage project: %s, %d areas, %d loads, %d variables, and %d shades',
                     self._name, len(self._vid_to_area.keys()), len(self._vid_to_load.keys()),
                     len(self._vid_to_variable.keys()), len(self._vid_to_shade.keys()))

        return True


class _RequestHelper():
    """A class to help with sending queries to the controller and waiting for
    responses.

    It is a wrapper used to help with executing a user action
    and then waiting for an event when that action completes.

    The user calls request() and gets back a threading.Event on which they then
    wait.

    If multiple clients of a vantage object (say an Output) want to get a status
    update on the current brightness (output level), we don't want to spam the
    controller with (near)identical requests. So, if a request is pending, we
    just enqueue another waiter on the pending request and return a new Event
    object. All waiters will be woken up when the reply is received and the
    wait list is cleared.

    NOTE: Only the first enqueued action is executed as the assumption is that the
    queries will be identical in nature.
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


class VantageEntity():
    """Base class for all the Vantage objects we'd like to manage. Just holds basic
    common info we'd rather not manage repeatedly."""

    def __init__(self, vantage, name, area, vid):
        """Initializes the base class with common, basic data."""
        self._vantage = vantage
        self._name = name
        self._area = area
        self._vid = vid

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
            area = self._vantage._vid_to_area.get(avid, None)
            if area is None:
                break
            areas.append(area.name)
            avid = area.parent
            if avid == 0:
                break
        areas = areas[::-1]
        areas.append(self._name)
        return areas

    def handle_update(self, _):
        """The handle_update callback is invoked when an event is received
        for the this entity.

        Returns:
            self - If event was valid and was handled.
            None - otherwise.
        """
        return None

    def is_output(self):
        """Return true iff this is an output."""
        return False

class Output(VantageEntity):
    """This is the output entity in Vantage universe. This generally refers to a
    switched/dimmed load, e.g. light fixture, outlet, etc."""
    CMD_TYPE = 'LOAD'
    ACTION_ZONE_LEVEL = 1
    _wait_seconds = 0.03  # TODO:move this to a parameter

    def __init__(self, vantage, name, area, output_type, load_type, cc_vid, dmx_color, vid):
        """Initializes the Output."""
        super(Output, self).__init__(vantage, name, area, vid)
        self._output_type = output_type
        self._load_type = load_type
        self._level = 0.0
        self._color_temp = 2700
        self._rgb = [0, 0, 0]
        self._hs = [0, 0]
        # if _load_type == 'COLOR' then _color_control_vid is the load's vid, else
        # it's the color control vid
        self._color_control_vid = cc_vid
        self._dmx_color = dmx_color
        self._query_waiters = _RequestHelper()
        self._vantage.register_id(Output.CMD_TYPE,
                                  "STATUS" if dmx_color else None,
                                  self)
        self._addedstatus = False

    def __str__(self):
        """Returns a pretty-printed string for this object."""
        return 'Output name: "%s" area: %d type: "%s" load: "%s" vid: %d %s%s%s [%s]' % (
            self._name, self._area, self._output_type, self._load_type, self._vid,
            ("(dim) " if self.is_dimmable else ""),
            ("(ctemp) " if self.support_color_temp else ""),
            ("(color) " if self.support_color else ""),
            self.full_lineage)

    def __repr__(self):
        """Returns a stringified representation of this object."""
        return str({'name': self._name, 'vid': self._vid, 'area': self._area,
                    'type': self._load_type, 'load': self._load_type,
                    'supports':
                    ("ctemp " if self.support_color_temp else "") +
                    ("color " if self.support_color else "")})

    @property
    def simple_name(self):
        """Return a simple pretty-printed string for this object."""
        return 'VID:%d (%s) [%s]' % (self._vid, self._name, self._load_type)

    # ADDSTATUS
    # DELSTATUS
    # S:STATUS [vid] RGBLoad.GetRGB [val] [ch[012]]
    # S:STATUS [vid] RGBLoad.GetRGBW [val] [ch[0123]]
    # S:STATUS [vid] RGBLoad.GetHSL [val] [ch[012]]
    # S:STATUS [vid] RGBLoad.GetColor [value]
    # S:STATUS [vid] RGBLoad.GetColorName [value]
    # INVOKE [vid] RGBLoad.SetRGBW [val0], [val1], [val2], [val3]
    def handle_update(self, args):
        """Handles an event update for this object, e.g. dimmer level change."""
        _LOGGER.debug("vantage - handle_update %d -- %s", self._vid, args)
        if len(args) == 1:
            level = float(args[0])
            if self._output_type == 'COLOR':
                color_temp = level_to_kelvin(level)
                light = self._vantage._vid_to_load.get(self._color_control_vid, None)
                if light:
                    light._color_temp = color_temp
                    _LOGGER.info("Received color change of VID %d set load VID %d to color = %d",
                                 self._vid, self._color_control_vid, color_temp)
                    light._query_waiters.notify()
                    return light
                _LOGGER.warning("Received color change of VID %d but cannot "
                                "find corresponding load", self._vid)
                return self
            _LOGGER.debug("Updating brightness %d(%s): l=%f",
                          self._vid, self._name, level)
            self._level = level
            self._query_waiters.notify()
        else:
            if args[0] == 'RGBLoad.GetRGB':
                _LOGGER.warning("RGBLoad.GetRGB, handling vid = %d; RGBW %s %s",
                                self._vid, args[1], args[2])
                val = int(args[1])
                char = int(args[2])
                if char < 3:
                    self._rgb[char] = val
                if char == 2:
                    self._query_waiters.notify()
        return self

    def __do_query_level(self):
        """Helper to perform the actual query the current dimmer level of the
        output. For pure on/off loads the result is either 0.0 or 100.0."""
#    self._vantage.send(Vantage.OP_QUERY, Output.CMD_TYPE, self._vid,
#            Output.ACTION_ZONE_LEVEL)
#    if self.support_color_temp or self.support_color:
        if self.support_color and not self._addedstatus:
            self._vantage.send("ADDSTATUS", self._vid)
            self._addedstatus = True
        _LOGGER.debug("getload of %s", self._vid)
        self._vantage.send("GETLOAD", self._vid)

    def last_level(self):
        """Returns last cached value of the output level, no query is performed."""
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

    @property
    def level(self):
        """Returns the current output level by querying the remote controller."""
        ev = self._query_waiters.request(self.__do_query_level)
        ev.wait(self._wait_seconds)
        return self._level

    @level.setter
    def level(self, new_level):
        """Sets the new output level."""
        if self._level == new_level:
            return
#    self._vantage.send(Vantage.OP_EXECUTE, Output.CMD_TYPE, self._vid,
#        Output.ACTION_ZONE_LEVEL, "%.2f" % new_level)
        self._vantage.send("LOAD", self._vid, str(new_level))
        self._level = new_level

    @property
    def rgb(self):
        """Returns current color of the light."""
        return self._rgb

    @rgb.setter
    def rgb(self, new_rgb):
        """Sets new color for the light."""
        if self._rgb == new_rgb:
            return
        # we need to adjust the rgb values to take into account the level
        r = self._level/100
        _LOGGER.info("rgb = %s", json.dumps(new_rgb))
        # INVOKE [vid] RGBLoad.SetRGBW [val0], [val1], [val2], [val3]
        self._vantage.send("INVOKE", self._vid,
                           ("RGBLoad.SetRGBW %d %d %d %d" %
                            (new_rgb[0]*r, new_rgb[1]*r, new_rgb[2]*r, 0)))
        srgb = sRGBColor(*new_rgb)
        hs_color = convert_color(srgb, HSVColor)
        self._hs = [hs_color.hsv_h, hs_color.hsv_s]
        self._rgb = new_rgb

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
        _LOGGER.info("hs = %s", json.dumps(new_hs))
        hs_color = HSVColor(new_hs[0], new_hs[1], 1.0)
        rgb = convert_color(hs_color, sRGBColor)
        self._vantage.send("INVOKE", self._vid,
                           "RGBLoad.SetRGBW %d %d %d %d" %
                           (rgb.rgb_r, rgb.rgb_g, rgb.rgb_b, 0))
        self._rgb = [rgb.rgb_r, rgb.rgb_g, rgb.rgb_b]
        self._hs = new_hs


    @property
    def color_temp(self):
        """Returns the current output level by querying the remote controller."""
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
            _LOGGER.info("Ignoring call to setter for color_temp of dmx_color light %d",
                         self._vid)
        else:
            self._vantage.send("LOAD", self._color_control_vid,
                               str(kelvin_to_level(new_color_temp)))
        self._color_temp = new_color_temp

## At some later date, we may want to also specify fade and delay times
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
    def type(self):
        """Returns the output type. At present AUTO_DETECT or NON_DIM."""
        return self._output_type

    @property
    def is_dimmable(self):
        """Returns a boolean of whether or not the output is dimmable."""
        return self._load_type.lower().find("non-dim") == -1

    def is_output(self):
        return True


class Button(VantageEntity):
    """This object represents a keypad button that we can trigger and handle
    events for (button presses)."""

    CMD_TYPE = 'LED' # for a button

    def __init__(self, vantage, name, area, vid, num, parent):
        super(Button, self).__init__(vantage, name, area, vid)
        self._num = num
        self._parent = parent
        self._vantage.register_id(Button.CMD_TYPE, None, self)

    def __str__(self):
        """Pretty printed string value of the Button object."""
        return 'Button name: "%s" num: %d area: %d vid: %d parent: %d' % (
            self._name, self._num, self._area, self._vid, self._parent)

    def __repr__(self):
        """String representation of the Button object."""
        return str({'name': self._name, 'num': self._num,
                    'area': self._area, 'vid': self._vid})

    @property
    def number(self):
        """Returns the button number."""
        return self._num

    def handle_update(self, args):
        """The callback invoked by the main event loop if there's an event from this keypad."""
        component = int(args[0])
        action = int(args[1])
#    params = [int(x) for x in args[2:]]
        _LOGGER.debug("Updating %d(%s): c=%d a=%d params=%s",
                      self._vid, self._name, component, action, args[2:])
        return self

class LoadGroup(Output):
    """Represent a Vantage LoadGroup."""
    def __init__(self, vantage, name, area, load_vids, dmx_color, vid):
        """Initialize a load group"""
        super(LoadGroup, self).__init__(vantage, name, area, 'GROUP', 'GROUP', None, dmx_color, vid)
        self._load_vids = load_vids

    def __str__(self):
        """Returns a pretty-printed string for this object."""
        return 'Output name: "%s" area: %d type: "%s" load: "%s" id: %d %s%s%s (%s) [%s]' % (
            self._name, self._area, self._output_type, self._load_type, self._vid,
            ("(dim) " if self.is_dimmable else ""),
            ("(ctemp) " if self.support_color_temp else ""),
            ("(color) " if self.support_color else ""),
            self._load_vids,
            self.full_lineage)

class Keypad(VantageEntity):
    """Object representing a Vantage keypad.

    Currently we don't really do much with it except handle the events
    (and drop them on the floor).
    """
    CMD_TYPE = 'KEYPAD' # for a keypad

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

    def handle_update(self, args):
        """The callback invoked by the main event loop if there's an event from this keypad."""
        component = int(args[0])
        action = int(args[1])
        params = [int(x) for x in args[2:]]
        _LOGGER.debug("Updating %d(%s): c=%d a=%d params=%s",
                      self._vid, self._name, component, action, params)
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

    def handle_update(self, args):
        """The callback invoked by the main event loop if there's an event from this keypad."""
        component = int(args[0])
        action = int(args[1])
        params = [int(x) for x in args[2:]]
        _LOGGER.debug("Updating %d(%s): c=%d a=%d params=%s",
                      self._vid, self._name, component, action, params)
        return self


class MotionSensor():
    """Placeholder class for the motion sensor device.

    TODO: Actually implement this.
    """
    def __init__(self, vantage, name, area, vid):
        """Initializes the motion sensor object."""
        self._vantage = vantage
        self._name = name
        self._area = area
        self._vid = vid


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
        self._sensors = []

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

    def add_sensor(self, sensor):
        """Adds a motion sensor object that's part of this area, only used during
        initial parsing."""
        self._sensors.append(sensor)

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


class Variable(VantageEntity):
    """A variable in the vantage system. See set_variable_vid.

    """
    CMD_TYPE = 'VARIABLE'  #GMem in the XML config

    def __init__(self, vantage, name, vid):
        """Initializes the variable object."""
        super(Variable, self).__init__(vantage, name, None, vid)
        self._value = None
        self._vantage.register_id(Variable.CMD_TYPE, None, self)

    def __str__(self):
        """Returns pretty-printed representation of this object."""
        return 'Variable name: "%s", vid: %d, value: %s' % (
            self._name, self._vid, self._value)

    @property
    def value(self):
        """The value of the variable."""
        return self._value

    def handle_update(self, args):
        """The callback invoked by the main event loop if there's a new value for the variable."""
        value = int(args[0])
        _LOGGER.info("Setting variable %s (%d) to %s", self._name, self._vid, str(value))
        self._value = value
        return self



class Shade(VantageEntity):
    """A shade in the vantage system.

    """
    CMD_TYPE = 'BLIND'  #MechoShade.IQ2_Shade_Node_CHILD in the XML config
    _wait_seconds = 0.03  # TODO:move this to a parameter

    def __init__(self, vantage, name, area_vid, vid):
        """Initializes the shade object."""
        super(Shade, self).__init__(vantage, name, area_vid, vid)
        self._level = 100
        self._load_type = 'BLIND'
        self._vantage.register_id(Shade.CMD_TYPE, None, self)
        self._query_waiters = _RequestHelper()

    @property
    def type(self):
        """Returns the output type. At present AUTO_DETECT or NON_DIM."""
        return self._load_type

    def __str__(self):
        """Returns pretty-printed representation of this object."""
        return 'Shade name: "%s", vid: %d, area: %d, level: %s' % (
            self._name, self._vid, self._area, self._level)

    def __repr__(self):
        """Returns a stringified representation of this object."""
        return str({'name': self._name, 'area': self._area,
                    'type': self._load_type, 'vid': self._vid})

    def last_level(self):
        """Returns last cached value of the output level, no query is performed."""
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
            if not new_level is None:
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

    def handle_update(self, args):
        """The callback invoked by the main event loop if there's a new value for the shade."""
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
        _LOGGER.info("Setting shade %s (%d) to float %s", self._name, self._vid, str(value))
        self._level = value
        return self
