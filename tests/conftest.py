"""Shared test fixtures.

An autouse fixture makes ``socket.socket.connect`` raise, so any accidental
network access from a test fails loudly instead of reaching the real network.
"""

import socket

import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise RuntimeError("network access is disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
