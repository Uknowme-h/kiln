"""Agent package. Each role is a thin module added on its scheduled day."""

from kiln.agents.roles import OPEN_WEIGHT_ROLES, SPARK_ROLES, Role

__all__ = ["OPEN_WEIGHT_ROLES", "Role", "SPARK_ROLES"]
