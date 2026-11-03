"""Mechanical product-quality checks (served UI, runtime smoke)."""

from kiln.product.served_ui_audit import audit_served_ui
from kiln.product.runtime_smoke import run_runtime_smoke

__all__ = ["audit_served_ui", "run_runtime_smoke"]
