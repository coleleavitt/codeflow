"""``python -m codeflow``: the command line (``codeflow cfg ...``, ``codeflow mcp``)."""

import sys

from .cli import main

sys.exit(main())
