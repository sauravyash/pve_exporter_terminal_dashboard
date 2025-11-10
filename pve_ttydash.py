#!/usr/bin/env python3
"""
pve_ttydash.py
Thin wrapper around dash_engine.run_dashboard().
Usage:
  PVE_DASH_CFG=./pve_dashboard.yaml TTY_DEV=/dev/tty python3 pve_ttydash.py
"""

import os
from dash_engine import run_dashboard

if __name__ == "__main__":
    cfg = os.environ.get("PVE_DASH_CFG", "config.yml")
    tty = os.environ.get("TTY_DEV", "/dev/tty")
    run_dashboard(cfg, tty)
