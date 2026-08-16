"""`python -m phc` entry point, equivalent to the `phc` console command.

Exists so the package can be run straight from a source checkout without
installing anything -- the role the old repo-root `phc.py` script played
before `core`/`devices`/`extensions` moved under this package. A root
`phc.py` can no longer serve that purpose: a module and a package of the
same name in the same directory shadow each other, so `phc.py` sitting
next to `phc/` would make `import phc.core` ambiguous.
"""

import sys

from phc.cli import main

if __name__ == "__main__":
    sys.exit(main())
