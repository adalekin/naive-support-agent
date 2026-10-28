"""The assistant's own tools: commands over its own state, never over the organization's systems.

Each command checks its input and either does what it says or refuses with the reason: what
the assistant may do is decided here, in code, not by the model following a prompt.
"""

import datetime
import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, update

from internal.access.guard import AccessGuard, Staff
from internal.access.oidc import ProviderUnavailable
from internal.agent.answer import SUBMIT_ANSWER_SCHEMA, Answer
from internal.agent.tools import Refused, RunnableTool, nullable, strict_object
from internal.bot.outbox import Outbox, Send
from internal.db import Database
from internal.entity import Case, CaseState, Incident, JournalEntry, JournalKind
from internal.privacy.pseudonymizer import LABEL, LabelMap
from internal.privacy.render import TextBuilder
from internal.privacy.rich import RichMessageBuilder
from internal.repository import CaseRepository, IncidentRepository, Journal
from internal.sources.base import SourceGateway
from internal.telegram import Text
from internal.words import Words


@dataclass
class CaseContext:
    """Everything a turn of a case works with.

    The case is held in memory for the turn and saved once at its end. Each command opens a
    unit of work of its own: a turn takes minutes, and no row stays locked for that long.
    """

    database: Database
    case: Case
    staff: Staff
    labels: LabelMap
    gateway: SourceGateway
    guard: AccessGuard
    words: Words
    outbox: Outbox
    now: datetime.datetime
    #: Labels that came from the request itself: a client is identified by one of them.
    request_labels: set[str]
    #: Words of the request, for a client identified by a number the staff wrote.
    request_text: str
    #: The message the turn answers, so that what the assistant says lands in the thread.
    reply_to: int
    thread_id: int | None
    #: The responsible person who answered in the thread, when the turn is about their answer.
    rule_for: Staff | None = None
    answer: Answer | None = None
    escalated: bool = False


@dataclass
class CaseCommands:
    context: CaseContext
    _done: set[str] = field(default_factory=set)

    def tools(self) -> list[RunnableTool]:
        tools = [
            RunnableTool(
                "set_case_state",
                "Record what the case waits for now. investigating: you are still on it. waiting_client: an answer "
                "of the client. waiting_fix: a fix of the organization reaching the clients; say in waiting_for what "
                "fix, so it can be checked later. waiting_owner: a decision of a responsible person. resolved: "
                "nothing, the case is closed.",
                strict_object(
                    {
                        "state": {"type": "string", "enum": [state.value for state in CaseState]},
                        "waiting_for": nullable("string", "What exactly the case waits for."),
                    }
                ),
                self.set_case_state,
                strict=True,
            ),
            RunnableTool(
                "identify_client",
                "Record which account of the product the case is about, once the sources found exactly one account "
                "by something the request gives (an email label, a phone label, an id). Data of other clients may "
                "not be shown.",
                strict_object(
                    {
                        "account": {"type": "string", "description": "The account id as the sources returned it."},
                        "found_by": {
                            "type": "string",
                            "description": "What from the request it was found by: a label like <<email_1>> or "
                            "an id the request contains.",
                        },
                    }
                ),
                self.identify_client,
                strict=True,
            ),
            RunnableTool(
                "link_incident",
                "Link the case to the failure it comes from, so that cases with one cause make one incident and "
                "the responsible person is called once. Call it when you found an organization's failure that may "
                "touch other clients. Returns how many cases the incident has and whether it was escalated.",
                strict_object(
                    {
                        "cause": {
                            "type": "string",
                            "description": "A short stable name of the cause, the same for every case of it.",
                        },
                        "description": {"type": "string", "description": "What fails, in plain words."},
                    }
                ),
                self.link_incident,
                strict=True,
            ),
            RunnableTool(
                "escalate",
                "Call the responsible people, in two cases: the cause could not be established from the data, or "
                "a decision is needed that the instructions do not give. Refused when the incident of the case was "
                "already escalated.",
                strict_object(
                    {
                        "complaint": {"type": "string", "description": "What the client complains about."},
                        "checked": {"type": "string", "description": "What you checked."},
                        "found": {"type": "string", "description": "What you found."},
                        "question": {
                            "type": "string",
                            "description": "What remained unclear, or what decision is needed.",
                        },
                    }
                ),
                self.escalate,
                strict=True,
            ),
            RunnableTool(
                "list_open_cases",
                "The open cases of the person who asks, with what each waits for.",
                strict_object({}),
                self.list_open_cases,
                strict=True,
            ),
            RunnableTool(
                "submit_answer",
                "Give your answer to the person who asked. Call it exactly once, at the end of the turn.",
                SUBMIT_ANSWER_SCHEMA,
                self.submit_answer,
                strict=True,
            ),
        ]
        if self.context.rule_for is not None:
            tools.append(
                RunnableTool(
                    "propose_rule",
                    "Offer the responsible person who answered the text of a rule for the instructions, by which "
                    "such a case is solved without them next time. The instructions are not changed by this.",
                    strict_object({"rule": {"type": "string"}}),
                    self.propose_rule,
                    strict=True,
                )
            )
        if self.context.staff.is_owner:
            tools.append(
                RunnableTool(
                    "summary",
                    "Numbers for a period, for a responsible person: cases taken, solved without a responsible "
                    "person, escalations and their reasons, answers rated as not helpful.",
                    strict_object({"days": {"type": "integer", "description": "How many days back."}}),
                    self.summary,
                    strict=True,
                )
            )
        return tools

    async def set_case_state(self, data: dict[str, Any]) -> str:
        state = CaseState(data["state"])
        waiting_for = (data.get("waiting_for") or "").strip() or None
        if state is CaseState.WAITING_FIX and waiting_for is None:
            raise Refused("waiting_fix needs waiting_for: which fix the case waits for.")
        if state is CaseState.WAITING_OWNER and not await self._owner_called():
            raise Refused(
                "No responsible person was called about this case: waiting_owner is only for a case a call went out "
                "for. Tell the person who asked that the call did not go through."
            )
        self.context.case.state = state
        self.context.case.waiting_for = waiting_for
        return f"The case is now {state.value}."

    async def _owner_called(self) -> bool:
        """A call went out in this turn, earlier in the case, or for the incident the case belongs to."""
        case = self.context.case
        if self.context.escalated or case.state is CaseState.WAITING_OWNER:
            return True
        if case.incident_id is None:
            return False
        async with self.context.database.unit_of_work() as session:
            incident = await session.get(Incident, case.incident_id)
        return incident is not None and incident.escalated_at is not None

    async def identify_client(self, data: dict[str, Any]) -> str:
        case = self.context.case
        account = data["account"].strip()
        found_by = data["found_by"].strip()
        if case.client_account is not None and case.client_account != account:
            raise Refused(
                "The case is about another client. Data of another client is not shown; "
                "a case about them is a separate request."
            )
        if found_by not in self.context.request_labels and found_by not in self.context.request_text:
            raise Refused(f"{found_by} is not in the request: a client is identified by what the request gives.")
        if not any(account in output and found_by in output for output in self.context.gateway.outputs):
            raise Refused(f"No answer of a source in this turn shows {account} together with {found_by}.")
        case.client_account = account
        return f"The case is about the account {account}."

    async def link_incident(self, data: dict[str, Any]) -> str:
        cause = " ".join(data["cause"].lower().split())
        case = self.context.case
        async with self.context.database.unit_of_work() as session:
            incidents = IncidentRepository(session)
            incident = await incidents.by_cause(cause) or await incidents.add(cause, data["description"].strip())
            # Linked at once, so that the next case of the same cause counts this one.
            await session.execute(update(Case).where(Case.id == case.id).values(incident_id=incident.id))
            case.incident_id = incident.id
            cases = await session.scalar(select(func.count(Case.id)).where(Case.incident_id == incident.id))
        return json.dumps(
            {
                "incident": incident.id,
                "cases": cases,
                "escalated_at": incident.escalated_at.isoformat() if incident.escalated_at else None,
            }
        )

    async def escalate(self, data: dict[str, Any]) -> str:
        context = self.context
        case = context.case
        if "escalate" in self._done:
            raise Refused("The responsible people are already called in this turn.")
        try:
            everyone = await context.guard.escalation_targets()
        except ProviderUnavailable as exc:
            raise Refused(
                "The sign-in provider does not answer, so the responsible people cannot be found now. "
                "Tell the person who asked."
            ) from exc
        # A responsible person who asks already knows the question: the others are called.
        asker = context.staff.identity.subject
        owners = [owner for owner in everyone if owner.identity.subject != asker]

        async with context.database.unit_of_work() as session:
            journal = Journal(session)
            # The incident is locked while it is checked and marked: two cases of one failure call once.
            incident = await IncidentRepository(session).get(case.incident_id) if case.incident_id else None
            if incident is not None and incident.escalated_at is not None:
                raise Refused(
                    f"The incident was escalated at {incident.escalated_at.isoformat()}; do not call again. "
                    "Refer to that call in the answer and say how many clients it touches."
                )
            if not owners:
                context.outbox.add(
                    Send(
                        case.chat_id,
                        Text(context.words.say("only_you" if everyone else "nobody_to_call")),
                        reply_to=context.reply_to,
                        thread_id=context.thread_id,
                        case_id=case.id,
                    )
                )
                await journal.write(
                    JournalKind.ESCALATED,
                    subject=context.staff.identity.subject,
                    case_id=case.id,
                    client_account=case.client_account,
                    nobody=True,
                    question=data["question"],
                )
                self._done.add("escalate")
                if everyone:
                    return "The person who asked is the only responsible person; they are told so."
                return "Nobody holds the role of a responsible person; the person who asked is told so."

            if incident is not None:
                incident.escalated_at = context.now
            await journal.write(
                JournalKind.ESCALATED,
                subject=context.staff.identity.subject,
                case_id=case.id,
                client_account=case.client_account,
                incident_id=incident.id if incident else None,
                question=data["question"],
                owners=[owner.identity.subject for owner in owners],
            )
        for message in self._escalation_messages(owners, data):
            context.outbox.add(message)
        case.state = CaseState.WAITING_OWNER
        context.escalated = True
        self._done.add("escalate")
        return f"Called {len(owners)} responsible person(s)."

    def _escalation_messages(self, owners: list[Staff], data: dict[str, Any]) -> list[Send]:
        context = self.context
        words = context.words

        def body(builder: RichMessageBuilder) -> RichMessageBuilder:
            builder.heading(words.say("escalation"), size=3)
            for key, field_name in (
                ("escalation_complaint", "complaint"),
                ("escalation_checked", "checked"),
                ("escalation_found", "found"),
                ("escalation_question", "question"),
            ):
                builder.heading(words.say(key)).markdown(data[field_name])
            return builder

        if not context.case.private:
            builder = RichMessageBuilder(context.labels).mentions(
                [(owner.binding.telegram_user_id, owner.binding.account_name) for owner in owners]
            )
            return [
                Send(
                    context.case.chat_id,
                    body(builder).build(),
                    reply_to=context.reply_to,
                    thread_id=context.thread_id,
                    case_id=context.case.id,
                )
            ]
        # A case of a private chat: each responsible person gets a private message, with who asked.
        messages = []
        for owner in owners:
            builder = body(RichMessageBuilder(context.labels))
            builder.paragraph(words.say("escalation_asked_by", account=context.staff.binding.account_name))
            messages.append(Send(owner.binding.telegram_user_id, builder.build(), case_id=context.case.id))
        return messages

    async def propose_rule(self, data: dict[str, Any]) -> str:
        owner = self.context.rule_for
        if owner is None:
            raise Refused("A rule is proposed only after a responsible person answered.")
        text = (
            TextBuilder(self.context.labels)
            .plain(self.context.words.say("rule_proposal"))
            .plain("\n\n")
            .styled("pre", data["rule"].strip())
            .build()
        )
        self.context.outbox.add(Send(owner.binding.telegram_user_id, text))
        return "The rule is offered to the responsible person; the instructions are unchanged."

    async def list_open_cases(self, data: dict[str, Any]) -> str:
        async with self.context.database.unit_of_work() as session:
            cases = await CaseRepository(session).open_of(self.context.staff.identity.subject)

        def relabelled(case: Case, text: str | None) -> str | None:
            # A label means a value only within its own case: the same <<email_1>> is another
            # person in this one. Each label is carried over to this case's label of its value.
            if text is None:
                return None
            theirs = LabelMap(case.labels)

            def carried(match: re.Match[str]) -> str:
                value = theirs.value(match.group())
                if value is None:
                    raise ValueError(f"case {case.id} holds a label it has no value for")
                return self.context.labels.label(match.group(1), value)

            return LABEL.sub(carried, text)

        return json.dumps(
            [
                {
                    "case": case.id,
                    "state": case.state.value,
                    "waiting_for": relabelled(case, case.waiting_for),
                    "client_account": relabelled(case, case.client_account),
                    "opened": case.created_at.date().isoformat(),
                }
                for case in cases
                if case.id != self.context.case.id
            ],
            ensure_ascii=False,
        )

    async def summary(self, data: dict[str, Any]) -> str:
        if not self.context.staff.is_owner:
            raise Refused("Only a responsible person gets the summary.")
        days = max(1, min(int(data["days"]), 366))
        since = self.context.now - datetime.timedelta(days=days)
        async with self.context.database.unit_of_work() as session:
            entries = list(
                await session.scalars(
                    select(JournalEntry).where(
                        JournalEntry.at >= since,
                        JournalEntry.kind.in_([JournalKind.REQUEST, JournalKind.ESCALATED, JournalKind.FEEDBACK]),
                    )
                )
            )
            requested = {entry.case_id for entry in entries if entry.kind is JournalKind.REQUEST}
            resolved = set(
                await session.scalars(select(Case.id).where(Case.id.in_(requested), Case.state == CaseState.RESOLVED))
            )
        escalations = [entry for entry in entries if entry.kind is JournalKind.ESCALATED]
        escalated_cases = {entry.case_id for entry in escalations}
        return json.dumps(
            {
                "days": days,
                "cases_taken": len(requested),
                "resolved_without_responsible_person": len(resolved - escalated_cases),
                "escalations": len(escalations),
                "escalation_reasons": [entry.details.get("question") for entry in escalations],
                "answers_rated_not_helpful": sum(
                    1
                    for entry in entries
                    if entry.kind is JournalKind.FEEDBACK and entry.details.get("helpful") is False
                ),
            },
            ensure_ascii=False,
        )

    async def submit_answer(self, data: dict[str, Any]) -> str:
        if self.context.answer is not None:
            raise Refused("The answer is already given.")
        try:
            self.context.answer = Answer.from_input(data)
        except ValueError as invalid:
            raise Refused(str(invalid)) from invalid
        return "The answer is given. End the turn."
