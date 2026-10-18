"""Endpoints: the Endpoint class, and building endpoints from specs.

endpoint.py is the runtime API; config.py expands endpoint specs
and samples histories. Re-exports endpoint.py.
"""

from phc.core.endpoint.endpoint import AGGREGATIONS, ON_INVALID_MODES, VALUE_TYPES, Endpoint
