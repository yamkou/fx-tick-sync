"""Run integration tests with Python socket egress restricted to loopback.

This is a test guard, not an OS sandbox for native libraries or subprocesses.
"""
import ipaddress
import os
from pathlib import Path
import sys
import unittest


def guard(event, args):
    if event in ('socket.connect', 'socket.bind', 'socket.sendto'):
        address = args[-1]
        host = address[0] if isinstance(address, tuple) else address
    elif event == 'socket.getaddrinfo':
        host = args[0]
    else:
        return
    try:
        allowed = ipaddress.ip_address(host).is_loopback
    except ValueError:
        allowed = host == 'localhost'
    if not allowed:
        raise PermissionError('Offline test guard rejected non-loopback socket operation')


if __name__ == '__main__':
    sys.addaudithook(guard)
    os.environ['STREAMLIT_BROWSER_GATHER_USAGE_STATS'] = 'false'
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    os.chdir(root)
    pattern = sys.argv[1] if len(sys.argv) > 1 else 'test_*.py'
    suite = unittest.defaultTestLoader.discover('tests', pattern=pattern)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
