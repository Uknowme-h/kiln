"""Permission rules as data: `{action, resource, effect}`, last match wins.

The shape follows OpenCode's permission model. A request names an action
(`read`, `edit`, `shell`, `install`, `web_fetch`, ...) and a resource (a path,
a command line, a URL). The ruleset decides `allow`, `ask`, or `deny`; a
request that matches nothing asks. Rules are plain data so each role can
declare its own and the companion can merge them with its defaults.

Pure functions only: no I/O lives here, so both the server (role definitions)
and the companion (enforcement) can import it.
"""

import re
from dataclasses import dataclass
from typing import Literal

Effect = Literal["allow", "ask", "deny"]


@dataclass(frozen=True)
class Rule:
    action: str
    resource: str
    effect: Effect


Ruleset = tuple[Rule, ...]


def wildcard_match(value: str, pattern: str) -> bool:
    """`*` matches any run of characters, `?` exactly one; the rest is literal."""
    regex = "".join(
        ".*" if char == "*" else "." if char == "?" else re.escape(char) for char in pattern
    )
    return re.fullmatch(regex, value, re.DOTALL) is not None


def evaluate(action: str, resource: str, *rulesets: Ruleset) -> Rule:
    """The last rule matching both action and resource; no match means ask."""
    chosen: Rule | None = None
    for ruleset in rulesets:
        for rule in ruleset:
            if wildcard_match(action, rule.action) and wildcard_match(resource, rule.resource):
                chosen = rule
    return chosen or Rule(action, "*", "ask")


def merge(*rulesets: Ruleset) -> Ruleset:
    return tuple(rule for ruleset in rulesets for rule in ruleset)


# Defaults every companion starts from. Specific rules come after the
# catch-all because the last match wins.
DEFAULT_RULES: Ruleset = (
    Rule("*", "*", "ask"),
    Rule("read", "*", "allow"),
    Rule("read", "*.env", "ask"),
    Rule("read", "*.env.*", "ask"),
    Rule("read", "*.env.example", "allow"),
    Rule("list", "*", "allow"),
    Rule("glob", "*", "allow"),
    Rule("grep", "*", "allow"),
    # Writes are bounded by path leases, so the rule itself allows them.
    Rule("edit", "*", "allow"),
    Rule("shell", "*", "ask"),
    Rule("install", "*", "ask"),
    Rule("probe", "*", "ask"),
    Rule("web_fetch", "*", "ask"),
    Rule("web_search", "*", "ask"),
    Rule("external_directory", "*", "ask"),
    Rule("question", "*", "allow"),
    # The plan-approval beat: the team never builds on an unreviewed plan.
    Rule("plan", "*", "ask"),
)


def role_rules(
    *,
    edit: tuple[str, ...] = (),
    deny_edit: tuple[str, ...] = (),
    shell: bool = False,
    web: bool = False,
) -> Ruleset:
    """Build a role ruleset: which paths it may edit, and whether it may shell out or browse.

    Anything not granted is denied, so a role never reaches for a tool it was
    not given even if its prompt is talked into asking.
    """
    rules: list[Rule] = [Rule("edit", "*", "deny")]
    rules += [Rule("edit", pattern, "allow") for pattern in edit]
    rules += [Rule("edit", pattern, "deny") for pattern in deny_edit]
    rules.append(Rule("shell", "*", "ask" if shell else "deny"))
    rules.append(Rule("install", "*", "ask" if shell else "deny"))
    for action in ("web_fetch", "web_search"):
        rules.append(Rule(action, "*", "ask" if web else "deny"))
    return tuple(rules)
