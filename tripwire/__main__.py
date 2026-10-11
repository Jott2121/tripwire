"""Enable `python -m tripwire`."""

import sys

from tripwire.cli import main

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
