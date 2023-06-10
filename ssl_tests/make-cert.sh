#!/bin/sh -
openssl req -x509 -nodes -days 365 -newkey rsa -keyout keyfile.key -out certfile.crt

