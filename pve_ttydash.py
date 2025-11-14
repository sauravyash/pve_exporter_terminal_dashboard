#!/usr/bin/env python3
"""
pve_ttydash.py
Thin wrapper around dash_engine.run_dashboard().
Usage:
  PVE_DASH_CFG=./pve_dashboard.yaml TTY_DEV=/dev/tty python3 pve_ttydash.py
"""

import os
import sys
from dash_engine import run_dashboard

if __name__ == "__main__":
    cfg = os.environ.get("CONFIG", "config.yml")
    tty = os.environ.get("TTY_DEV", "/dev/tty")
    try:
        run_dashboard(cfg, tty)
    except (KeyboardInterrupt, EOFError):
        print("\nExiting...")
        sys.exit(0)
