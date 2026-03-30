"""Pytest configuration for server tests."""

import sys
import os

# Add server/ to path so tests can import modules directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
