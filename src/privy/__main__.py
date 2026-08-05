"""Allow ``python -m privy`` and serve as the PyInstaller binary entrypoint."""

from privy.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
