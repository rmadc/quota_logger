"""Shared entry point used to build the three named Windows executables."""
from __future__ import annotations

import pathlib
import sys

from quota_logger import collector, config, history


def main() -> int:
    executable = pathlib.Path(sys.argv[0]).stem.casefold()
    if "history" in executable:
        if "--follow" not in sys.argv[1:] and "--no-follow" not in sys.argv[1:]:
            return history.main(["--follow", *sys.argv[1:]])
        return history.main()
    if "config" in executable:
        return config.app_main()
    return collector.main()


if __name__ == "__main__":
    raise SystemExit(main())
