"""Back-compat entry point: python -m fsgen.run"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
