pyvantage
=========
A simple Python library for controlling a Vantage Controls system for lighting, etc.

This started by copying pylutron under the MIT License on 2018-02-11 from https://github.com/thecynic/pylutron.
Updated for shades on 2018-08-20

Authors
-------
Greg Badros (gjbadros on github) built this package for the Vantage Controller lighting systems.

Dima Zavin (thecynic on github) wrote pylutron, on which this was originally based.



Installation
------------

Get the source from github.


Example
-------
    import pyvantage

    vcl = pyvantage.Vantage("192.168.0.x", "vantage", "integration")
    vcl.load_xml_db()
    vcl.connect()


License
-------
This code is released under the MIT license.
