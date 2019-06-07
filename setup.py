#!/usr/bin/env python

from setuptools import setup, find_packages

setup(
    name = 'pyvantage',
    version = '0.0.11',
    license = 'MIT',
    description = 'Python library for Vantage Controller, used for Home Assistant',
    author = 'Greg J. Badros',
    author_email = 'badros@gmail.com',
    url = 'http://github.com/gjbadros/pyvantage',
    packages=find_packages(),
    classifiers = [
        'Development Status :: 3 - Alpha',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3.4',
        'Topic :: Home Automation',
        'Topic :: Software Development :: Libraries :: Python Modules',
    ],
    install_requires=[],
    zip_safe=True,
)
