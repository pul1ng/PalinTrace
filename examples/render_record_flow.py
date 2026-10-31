"""Compatibility entry point; use palin record visualize RUN_DIR [--output FILE]."""

import sys

from palinreplay.cli import main as cli_main
from palinreplay.record.visualize import build_data

__all__ = ["build_data", "main"]


def main():
    return cli_main(["record", "visualize", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
