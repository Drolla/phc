"""recovery extension: persists selected writable endpoint values.

To a small YAML file, and restores them automatically at startup --
before the scheduler's first tick -- so critical device state survives
a crash/restart instead of falling back to each endpoint's hardcoded
default."""
