"""recovery extension: persists a selected set of writable device endpoint
values to a small YAML file, and restores them automatically at startup --
before the scheduler's first tick -- so critical device state survives a
crash/restart instead of falling back to each endpoint's hardcoded
default."""
