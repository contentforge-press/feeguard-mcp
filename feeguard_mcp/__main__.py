# -*- coding: utf-8 -*-
"""入口：python3 -m feeguard_mcp [--csv 路径 | --audit-log 路径]"""
import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
