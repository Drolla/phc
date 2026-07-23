"""Shared pytest fixtures/helpers for the PHC test suite."""

import asyncio


def fetch_sync(device):
    """Run device.fetch() to completion outside the Scheduler -- for tests
    that exercise a device directly (not through Scheduler.tick(), which
    already awaits fetch() on its own persistent event loop)."""
    asyncio.run(device.fetch())
