"""Kiln: a multi-agent dev team on Meta models."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("kiln")
except PackageNotFoundError:
    __version__ = "0.1.0"
