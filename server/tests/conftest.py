"""Pytest configuration for server tests."""

import os
import sys
from unittest.mock import MagicMock

# Add server/ to path so tests can import modules directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# The generated DSDL packages (uavcan.*) only exist once nnvg has compiled
# them into python_compiled_messages/, which is not on the test path. Stub
# them here, before any test module is collected, so every file imports the
# same mocked tree regardless of collection order or -k selection.
#
# pycyphal.application and pycyphal.dsdl must be stubbed too: both call
# get_model() on generated types at import time and assert the result is a
# real DSDL model, which a MagicMock uavcan cannot satisfy. The rest of
# pycyphal (notably pycyphal.transport, which test_frame_capture imports for
# real) is a normal installed dependency and is left alone.
for _name in [
    "uavcan", "uavcan.node", "uavcan.node.port", "uavcan.register",
    "uavcan.primitive", "uavcan.diagnostic", "uavcan.pnp",
    "pycyphal.application", "pycyphal.dsdl",
]:
    sys.modules.setdefault(_name, MagicMock())
