"""Test package bootstrap.

Two jobs: make the package importable without installation, and make the
"tests never touch the network" rule an enforced fact rather than a promise.
Any attempt to open a socket during the suite raises immediately.
"""

import os
import socket
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# The config that describes the two recorded pages. The suite runs against the
# same file a user would run against, so a change that breaks the config format
# breaks the tests rather than only breaking real users.
RECORDED_CONFIG = os.path.join(_ROOT, "examples", "recorded-course.ini")


def demo_config():
    """A fresh Config for the recorded course.

    Fresh every call because `main()` mutates it: --fall-year and --alarm-days
    are applied to the Config the run was handed.
    """
    from courseics.config import load_config
    return load_config(RECORDED_CONFIG)


class NetworkAccessDenied(RuntimeError):
    pass


def _blocked(*args, **kwargs):
    raise NetworkAccessDenied(
        "the test suite is forbidden from using the network")


# Let the stdlib finish wiring itself up BEFORE the block goes in: ssl defines
# `class SSLSocket(socket.socket)` at import time, so replacing socket.socket
# first would break the import rather than the network.
import ssl          # noqa: E402,F401
import http.client  # noqa: E402,F401
import urllib.request  # noqa: E402,F401

_REAL_SOCKET = socket.socket


class _DeniedSocket(_REAL_SOCKET):
    """Still a socket subclass (so isinstance checks hold), but unusable."""

    def __init__(self, *args, **kwargs):
        _blocked()


# Hard block: socket construction, name resolution, and direct connections.
socket.socket = _DeniedSocket           # type: ignore[assignment]
socket.create_connection = _blocked     # type: ignore[assignment]
socket.getaddrinfo = _blocked           # type: ignore[assignment]
