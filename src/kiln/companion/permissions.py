"""Companion-side enforcement of the permission rules.

The companion is the trust root: it decides, on the user's machine, whether a
tool call proceeds. Rules come from `kiln.permissions` (defaults, then the
calling role's ruleset on top, last match wins). An `ask` becomes a prompt in
this terminal; a `deny` is final; an `allow` just runs.

Approval can be remembered only for the rest of the current run, only for the
exact same command, and only where the caller says it is low-risk (the test
command, a version probe). Installs, network access, and anything the scanner
could not read are asked every time.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass

from kiln.companion.prompt import ApprovalPrompt, ApprovalRequest
from kiln.companion.protocol import ToolProgress
from kiln.permissions import DEFAULT_RULES, Ruleset, evaluate

Notify = Callable[[ToolProgress], None]


class PermissionDenied(RuntimeError):
    """Policy or the user said no. The message is returned to the agent as the tool error."""


@dataclass
class AuditEntry:
    agent: str
    action: str
    resource: str
    effect: str  # allow | deny | user-allow | user-deny | run-grant


class PermissionEngine:
    def __init__(
        self,
        prompt: ApprovalPrompt,
        rules: Ruleset = DEFAULT_RULES,
        audit_limit: int = 500,
    ):
        self.prompt = prompt
        self.rules = rules
        self._grants: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self.audit: list[AuditEntry] = []
        self._audit_limit = audit_limit

    def _record(self, agent: str, action: str, resource: str, effect: str) -> None:
        with self._lock:
            self.audit.append(AuditEntry(agent, action, resource, effect))
            del self.audit[: -self._audit_limit]

    def deny(self, agent: str, action: str, resource: str, reason: str) -> PermissionDenied:
        """Record a refusal made by code (not by a rule) and return the error to raise."""
        self._record(agent, action, resource, "deny")
        return PermissionDenied(reason)

    def authorize(
        self,
        *,
        agent_id: str,
        agent_label: str,
        action: str,
        resource: str,
        role_rules: Ruleset = (),
        request: ApprovalRequest | None = None,
        remember_key: str | None = None,
        notify: Notify | None = None,
        call_id: str = "",
    ) -> None:
        """Return if the call may proceed; raise `PermissionDenied` if it may not."""
        rule = evaluate(action, resource, self.rules, role_rules)
        who = agent_id or "orchestrator"
        if rule.effect == "deny":
            raise self.deny(who, action, resource, f"{action} is not allowed for {agent_label or who}")
        if rule.effect == "allow":
            self._record(who, action, resource, "allow")
            return
        grant = (action, remember_key) if remember_key else None
        with self._lock:
            granted = grant in self._grants if grant else False
        if granted:
            self._record(who, action, resource, "run-grant")
            return
        request = request or ApprovalRequest(agent_label or who, action, [resource])
        if notify is not None:
            notify(
                ToolProgress(
                    id=call_id or "approval",
                    state="awaiting_approval",
                    agent=agent_id,
                    action=action,
                    detail=f"{agent_label or who} is waiting for your approval to {action}",
                    # the exact request, so the web floor renders the same
                    # dialog the terminal shows
                    lines=list(request.lines),
                    note=request.note,
                    allow_run_scope=request.allow_run_scope,
                )
            )
        reply = self.prompt.ask(request)
        if notify is not None:
            notify(
                ToolProgress(
                    id=call_id or "approval",
                    state="approval_resolved",
                    agent=agent_id,
                    action=action,
                    approved=reply.approved,
                )
            )
        if not reply.approved:
            self._record(who, action, resource, "user-deny")
            message = f"the user refused this {action}"
            if reply.feedback:
                message += f": {reply.feedback}"
            raise PermissionDenied(message)
        self._record(who, action, resource, "user-allow")
        if reply.scope == "run" and grant and request.allow_run_scope:
            with self._lock:
                self._grants.add(grant)
