import pytest

from freshdesk_mcp import FreshdeskClient


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    timer = FakeTime()
    original = FreshdeskClient.__init__

    def initialize(self, **kwargs):
        kwargs.setdefault("sleep", timer.sleep)
        kwargs.setdefault("random_source", lambda: 0.0)
        kwargs.setdefault("clock", timer.clock)
        original(self, **kwargs)

    monkeypatch.setenv("FRESHDESK_CALLS_PER_MINUTE", "30")
    monkeypatch.setattr(FreshdeskClient, "__init__", initialize)
    return timer
