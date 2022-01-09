# pyvantage

A simple Python library for controlling a Vantage Controls system for lighting, etc.

The very first version of this started by copying pylutron under the
MIT License on 2018-02-11 from https://github.com/thecynic/pylutron.
It has evolved massively since then.

2018-08-20: Updated for shades
2019-12-11: Updated for multiple telnet connections on 2019-12-11
2020-03: Updated for better button support in 2020-03
2020-04-30: Updated for better variable support on 2020-04-30
2022-01-08: Updated for (partial) support for triplets of dry-contact closures ending in open/close/is open/stop
turning into shade3 objects that are blinds that allow opening, closing, stopping either (optionally)
and detecting if open. Also improved network retries when connection to the
vantage server is lost. Try handling IRZone Button presses, too.

(See git version history for details.)

## Authors

Greg Badros (gjbadros on github) built this package for the Vantage
Controller lighting systems. He is the primary author and maintainer
and welcomes contributions, including of Design Center .dc files for
testing purposes.

Chris Colohan (colohan on github) has made several improvements and
contributions including around button press handling.

Dima Zavin (thecynic on github) wrote pylutron, on which this was
originally based.

## Installation

Get the source from github. To use with Home Assistant, see the
https://github.com/gjbadros/hass-vantage package.

## Example

    import pyvantage

    vcl = pyvantage.Vantage("192.168.0.x", "vantage", "integration")
    vcl.load_xml_db()
    vcl.connect()

## License

This code is released under the MIT license.
