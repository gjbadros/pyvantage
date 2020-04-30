pyvantage
=========
A simple Python library for controlling a Vantage Controls system for lighting, etc.

The very first version of this started by copying pylutron under the
MIT License on 2018-02-11 from https://github.com/thecynic/pylutron.
It has evolved massively since then.

Updated for shades on 2018-08-20
Updated for multiple telnet connections on 2019-12-11
Updated for better button support in 2020-03 
Updated for better variable support on 2020-04-30

(See git version history for details.)

Authors
-------

Greg Badros (gjbadros on github) built this package for the Vantage
Controller lighting systems.  He is the primary author and maintainer
and welcomes contributions, including of Design Center .dc files for
testing purposes.

Chris Colohan (colohan on github) has made several improvements and
contributions including around button press handling.

Dima Zavin (thecynic on github) wrote pylutron, on which this was
originally based.



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
