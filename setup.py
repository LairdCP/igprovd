#!/usr/bin/python

from setuptools import setup

setup(
    name="igprovd",
    version="1.0",
    packages=["igprovd"],
    scripts=["scripts/igprovd", "scripts/ggconf", "scripts/edge_iq_config"],
)
