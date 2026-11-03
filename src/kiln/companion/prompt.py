"""The approval prompt: the user's one chance to say no.

Everything an agent wants to do that is not plainly safe ends up here. With
``remote=True`` (the companion's default) the question is also answered from
the web floor: the pending request is relayed to the browser, and the reply
comes back over the companion websocket. Whichever answer arrives first — the
terminal or the browser — wins; the prompt is still serialized by one lock, so
two agents asking at once never interleave.

Replies (terminal):
    y            allow this once
    a            allow this exact command for the rest of this run (low-risk only)
    n            deny
    n <reason>   deny, and tell the agent why; it reads this as the tool's error

With no interactive terminal and no remote channel, everything is denied: an
unattended run never approves anything by default.
"""

import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class ApprovalRequest:
    agent_label: str
    action: str  # shell, install, web_fetch, ...
    lines: list[str]  # the exact commands or targets, one per line
    shell: str = ""  # "PowerShell 7", "bash", ...
    workdir: str = ""
    note: str = ""  # why it is asked, e.g. "installs software"
    allow_run_scope: bool = False


@dataclass
class Reply:
    approved: bool
    scope: str = "once"  # "once" or "run"
    feedback: str = ""


@dataclass
class Question:
    header: str
    question: str
    options: list[str] = field(default_factory=list)
    multiple: bool = False


class ApprovalPrompt:
    """Serialized approval prompts. Injectable I/O keeps it testable.

    ``remote=True`` lets a reply arrive from another thread (the companion's
    websocket reader, relaying the web floor's answer) while a question is
    pending. A background thread feeds typed terminal lines into a queue so the
    wait can race both sources without two ``input()`` calls ever competing.
    """

    def __init__(
        self,
        input_fn: Callable[[str], str] | None = None,
        output: Callable[[str], None] | None = None,
        interactive: bool | None = None,
        remote: bool = False,
    ):
        self._input = input_fn or input
        self._custom_input = input_fn is not None
        self._out = output or print
        if interactive is None:
            interactive = input_fn is not None or bool(
                getattr(sys.stdin, "isatty", lambda: False)()
            )
        self.interactive = interactive
        # A custom input_fn is for tests and scripts: keep the old direct call,
        # no reader thread racing it.
        self._remote = remote and not self._custom_input
        self._lock = threading.Lock()
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._waiting = threading.Event()
        self._remote_reply: Reply | None = None
        self._remote_answers: list[list[str]] | None = None
        self._waiting_questions = threading.Event()

    # ── the web floor's answer ─────────────────────────────────────────────

    def answer_remotely(self, reply: Reply) -> bool:
        """Resolve the pending prompt from the web floor. False when nothing
        is waiting (the answer arrived too late and is dropped)."""
        if not self._waiting.is_set():
            return False
        self._remote_reply = reply
        self._waiting.clear()
        return True

    def answer_questions_remotely(self, answers: list[list[str]]) -> bool:
        """Resolve a pending ask_user questionnaire from the web floor."""
        if not self._waiting_questions.is_set():
            return False
        self._remote_answers = answers
        self._waiting_questions.clear()
        return True

    def _read_lines(self) -> None:
        """Daemon: one stdin reader for the process, so prompts never race on input()."""
        while True:
            try:
                self._lines.put(self._input("   > "))
            except (EOFError, KeyboardInterrupt, StopIteration):
                self._lines.put(None)  # the terminal went away
                return
            except Exception:
                return

    def _ensure_reader(self) -> None:
        if self._reader is None or not self._reader.is_alive():
            self._reader = threading.Thread(
                target=self._read_lines, name="approval-stdin", daemon=True
            )
            self._reader.start()

    def _drain_stale_lines(self) -> None:
        """Drop anything typed while no prompt was on screen, so an old line
        can never answer a new question."""
        while True:
            try:
                self._lines.get_nowait()
            except queue.Empty:
                return

    def _wait_for_reply(self, request: ApprovalRequest) -> Reply:
        """Race the terminal against the web floor; first answer wins."""
        self._remote_reply = None
        self._waiting.set()
        if self.interactive:
            self._ensure_reader()
            self._drain_stale_lines()
        try:
            while True:
                if not self._waiting.is_set():
                    reply = self._remote_reply or Reply(False, feedback="no answer")
                    self._out(
                        f"   …answered from the web floor: "
                        f"{'allowed' if reply.approved else 'denied'}"
                    )
                    return self._coerce(reply, request.allow_run_scope)
                try:
                    line = self._lines.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    return Reply(False, feedback="the approval prompt was closed; denied")
                return self._parse(line.strip(), request.allow_run_scope)
        finally:
            self._waiting.clear()

    @staticmethod
    def _coerce(reply: Reply, allow_run_scope: bool) -> Reply:
        """A run-scope grant is honored only where the request says low-risk."""
        if reply.scope == "run" and not allow_run_scope:
            reply = Reply(reply.approved, "once", reply.feedback)
        return reply

    # ── terminal rendering ─────────────────────────────────────────────────

    def ask(self, request: ApprovalRequest) -> Reply:
        if not self.interactive and not self._remote:
            return Reply(False, feedback="no interactive terminal to approve this; denied")
        with self._lock:
            self._out("")
            self._out(f"── approval needed ── {request.agent_label} wants to {request.action}")
            if request.shell or request.workdir:
                where = f"in {request.workdir}" if request.workdir else ""
                self._out(f"   {' '.join(part for part in (request.shell, where) if part)}")
            for index, line in enumerate(request.lines, start=1):
                prefix = f"{index})" if len(request.lines) > 1 else ">"
                self._out(f"   {prefix} {line}")
            if request.note:
                self._out(f"   note: {request.note}")
            if self.interactive:
                choices = "[y] allow once  "
                if request.allow_run_scope:
                    choices += "[a] allow this exact command for this run  "
                choices += "[n] deny  [n <reason>] deny and tell the agent why"
                self._out(f"   {choices}")
            if self._remote:
                self._out("   (or answer on the web floor)")
                return self._wait_for_reply(request)
            try:
                answer = self._input("   > ").strip()
            except (EOFError, KeyboardInterrupt):
                return Reply(False, feedback="the approval prompt was closed; denied")
        return self._parse(answer, request.allow_run_scope)

    @staticmethod
    def _parse(answer: str, allow_run_scope: bool) -> Reply:
        word, _, rest = answer.partition(" ")
        word = word.lower()
        if word in ("y", "yes"):
            return Reply(True, "once")
        if word in ("a", "always") and allow_run_scope:
            return Reply(True, "run")
        if word in ("n", "no"):
            return Reply(False, feedback=rest.strip())
        # anything unrecognized is a refusal; free text is useful feedback
        return Reply(False, feedback=answer.strip())

    def _next_line(self) -> str:
        """One typed line. When the remote channel owns a stdin reader thread,
        read from its queue so two input() calls never compete for the terminal."""
        if self._remote:
            self._ensure_reader()
            self._drain_stale_lines()
            line = self._lines.get()
            return (line or "").strip()
        try:
            return self._input("   > ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    def ask_questions(self, agent_label: str, questions: list[Question]) -> list[list[str]]:
        """Put the agent's questions to the user; returns one answer list per question.

        With ``remote=True``, the web floor can submit all answers at once and
        race the terminal. Terminal mode still answers one question at a time.
        """
        if not self.interactive and not self._remote:
            return [[] for _ in questions]
        with self._lock:
            self._out("")
            self._out(f"── questions from {agent_label} ──")
            for question in questions:
                self._out(f"   [{question.header}] {question.question}")
                for number, option in enumerate(question.options, start=1):
                    self._out(f"     {number}) {option}")
                hint = "number(s)" if question.multiple else "a number"
                if question.options:
                    self._out(f"   answer with {hint}, or type your own (blank to skip)")
                else:
                    self._out("   type your answer (blank to skip)")
            if self._remote:
                self._out("   (or answer on the web floor)")
                return self._wait_for_question_answers(questions)
            answers: list[list[str]] = []
            for question in questions:
                answers.append(self._resolve_answer(self._next_line(), question))
            return answers

    def _pad_remote_answers(
        self, questions: list[Question], remote: list[list[str]] | None
    ) -> list[list[str]]:
        if remote is None:
            return [[] for _ in questions]
        out: list[list[str]] = []
        for i, _q in enumerate(questions):
            if i < len(remote) and isinstance(remote[i], list):
                out.append([str(x) for x in remote[i]])
            else:
                out.append([])
        return out

    def _wait_for_question_answers(
        self, questions: list[Question]
    ) -> list[list[str]]:
        """Race terminal (per-question) against a full remote answer payload."""
        self._remote_answers = None
        self._waiting_questions.set()
        collected: list[list[str]] = []
        try:
            if self.interactive:
                self._ensure_reader()
                self._drain_stale_lines()
            for question in questions:
                while True:
                    if not self._waiting_questions.is_set():
                        self._out("   …answered from the web floor")
                        return self._pad_remote_answers(questions, self._remote_answers)
                    if not self.interactive:
                        # No stdin — park until the floor answers (same as shell).
                        time.sleep(0.1)
                        continue
                    try:
                        line = self._lines.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if line is None:
                        collected.append([])
                        break
                    collected.append(self._resolve_answer(line.strip(), question))
                    break
            return collected
        finally:
            self._waiting_questions.clear()

    @staticmethod
    def _resolve_answer(raw: str, question: Question) -> list[str]:
        if not raw:
            return []
        if not question.options:
            return [raw]
        picks: list[str] = []
        for token in raw.replace(",", " ").split():
            if token.isdigit() and 1 <= int(token) <= len(question.options):
                picks.append(question.options[int(token) - 1])
            else:
                return [raw]  # not a clean list of numbers: take the text as typed
            if not question.multiple:
                break
        return picks or [raw]
