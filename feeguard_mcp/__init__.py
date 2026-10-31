# -*- coding: utf-8 -*-
"""FeeGuard —— 电商结算表的费率异常巡检。判定全部是确定性数学。"""
from . import rules  # noqa: F401
from .rules import (  # noqa: F401
    DEFAULT_CFG, REQUIRED, audit_file, build_cells, detect_columns,
    load_rate_rows, norm_date, rule_ceiling, rule_jump, rule_outlier, scan,
)

__version__ = "0.1.0"
__all__ = ["rules", "audit_file", "scan", "DEFAULT_CFG", "__version__"]
