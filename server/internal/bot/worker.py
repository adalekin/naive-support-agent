"""Takes the turns of cases from the queue and runs the model over them.

Turns of one case go one after another, in the order they came; different cases go side by side,
up to a limit. A turn is marked done in the same transaction that saves what it found.
"""

import asyncio
import datetime
from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy import delete, select

from internal.access.guard import AccessGuard, Staff
from internal.access.oidc import ProviderUnavailable
from internal.agent.classifier import CaseClassifier
from internal.agent.commands import CaseContext
from internal.agent.runner import CaseAgent, TurnOutcome
from internal.bot.outbox import Outbox, Send, SendMedia
from internal.bot.postman import Postman
from internal.db import Database
from internal.entity import Case, CaseState, CaseTurn, JournalKind, MessageRole, StaffBinding, TurnKind
from internal.privacy.pseudonymizer import LABEL, LabelMap, Pseudonymizer
from internal.repository import CaseRepository, Journal
from internal.sources.attachments import AttachmentReader
from internal.sources.base import SourceRegistry
from internal.telegram import Attachment, Button, Text
from internal.words import Words

OPERATOR_NOTES = {
    TurnKind.RETRY: "The person who asked rated your previous answer as not helpful, and this message says what "
    "was wrong. Look into the case again with it.",
    TurnKind.OWNER_ANSWER: "A responsible person answered in the thread; this message is theirs. The thread is "
    "theirs now: nothing you submit is posted there. If their answer settles how such cases are solved, offer "
    "them a rule for the instructions with propose_rule. Then finish with submit_answer of kind reply.",
    TurnKind.FOLLOW_UP: "This is a scheduled check, not a message of the staff. The case waits for this fix: "
    "{waiting_for}. Check with the sources whether the fix is available to clients now, not only made. If it "
    "is, record the new state of the case and answer with kind reply that the fix is available and the client "
    "can be told. If it is not, keep the state waiting_fix and answer with kind reply in one line.",
}


#: Said to the model in every turn of a person who may ask for aggregate figures (``case-investigation``).
ANALYTICS_NOTE = (
    "The person who asks holds the role for aggregate questions: answer them with aggregate figures over the "
    "data (totals, counts, breakdowns by day or plan), never naming a client or showing a client's data."
)


@dataclass(frozen=True)
class RequestInput:
    content: list[dict[str, Any]]
    #: att_N to the Telegram file it stands for, so that the answer can show it again.
    attachments: dict[str, Attachment]
    labels: set[str]
    text: str


class CaseWorker:
    def __init__(
        self,
        database: Database,
        agent: CaseAgent,
        classifier: CaseClassifier,
        registry: SourceRegistry,
        reader: AttachmentReader,
        guard: AccessGuard,
        words: Words,
        personal_fields: frozenset[str],
        postman: Postman,
        concurrency: int,
    ) -> None:
        self._database = database
        self._agent = agent
        self._classifier = classifier
        self._registry = registry
        self._reader = reader
        self._guard = guard
        self._words = words
        self._personal_fields = personal_fields
        self._postman = postman
        self._slots = asyncio.Semaphore(concurrency)
        self._active: dict[int, asyncio.Task[None]] = {}
        self.wake = asyncio.Event()

    async def run_forever(self, idle_seconds: float = 5.0) -> None:
        while True:
            await self.start_pending()
            try:
                await asyncio.wait_for(self.wake.wait(), idle_seconds)
            except TimeoutError:
                pass
            self.wake.clear()

    async def start_pending(self) -> None:
        async with self._database.unit_of_work() as session:
            turns = await CaseRepository(session).pending_turns()
        for case_id in dict.fromkeys(turn.case_id for turn in turns):
            if case_id not in self._active:
                self._active[case_id] = asyncio.create_task(self._drain(case_id), name=f"case-{case_id}")

    async def drain_all(self) -> None:
        """Run every pending turn to the end: for the tests and for a clean stop."""
        await self.start_pending()
        while self._active:
            await asyncio.gather(*self._active.values())

    async def _drain(self, case_id: int) -> None:
        try:
            async with self._slots:
                while True:
                    async with self._database.unit_of_work() as session:
                        turn = await session.scalar(
                            select(CaseTurn)
                            .where(CaseTurn.case_id == case_id, CaseTurn.done_at.is_(None))
                            .order_by(CaseTurn.id)
                            .limit(1)
                        )
                    if turn is None:
                        return
                    try:
                        await self.process(turn)
                    except Exception:  # noqa: BLE001 - the top of a worker, see below
                        # The top of a case's worker: the failure is logged and told, the turn is
                        # closed so that it does not fail again and again, and the other cases go on.
                        logger.exception("The turn of case {} failed", case_id)
                        await self._finish(turn, self._failed(turn))
        finally:
            self._active.pop(case_id, None)

    async def process(self, turn: CaseTurn) -> None:
        payload = turn.payload
        async with self._database.unit_of_work() as session:
            case = await session.get(Case, turn.case_id)
        staff, unavailable = await self._staff(payload["staff_telegram_id"])
        outbox = Outbox()
        if case is None:
            return
        if staff is None:
            if unavailable and turn.kind is not TurnKind.FOLLOW_UP:
                outbox.say(payload["chat_id"], self._words.say("access_unavailable"), reply_to=payload["message_id"])
            await self._finish(turn, outbox)
            return
        if case.owner_took_over and turn.kind is not TurnKind.OWNER_ANSWER:
            await self._finish(turn, outbox)
            return

        labels = LabelMap(case.labels)
        pseudonymizer = Pseudonymizer(self._personal_fields, labels)
        if turn.kind is TurnKind.OWNER_MENTION:
            if not await self._classifier.is_case(pseudonymizer.mask_text(payload["text"])):
                # Colleagues talking: no case, no trace.
                async with self._database.unit_of_work() as session:
                    await session.execute(delete(Case).where(Case.id == case.id))
                return
            await self._postman.deliver(
                Outbox(
                    [
                        Send(
                            payload["chat_id"],
                            Text(self._words.say("taken")),
                            reply_to=payload["message_id"],
                            thread_id=payload["thread_id"],
                            case_id=case.id,
                        )
                    ]
                )
            )

        request = await self._request_input(turn, case, pseudonymizer)
        gateway = self._registry.gateway(pseudonymizer)
        context = CaseContext(
            database=self._database,
            case=case,
            staff=staff,
            labels=labels,
            gateway=gateway,
            guard=self._guard,
            words=self._words,
            outbox=outbox,
            now=datetime.datetime.now(datetime.UTC),
            request_labels=request.labels,
            request_text=request.text,
            reply_to=payload["message_id"],
            thread_id=payload["thread_id"],
            rule_for=staff if turn.kind is TurnKind.OWNER_ANSWER else None,
        )
        note = OPERATOR_NOTES.get(turn.kind)
        if note is not None and turn.kind is TurnKind.FOLLOW_UP:
            note = note.format(waiting_for=case.waiting_for)
        if staff.may_analyze:
            note = f"{note}\n\n{ANALYTICS_NOTE}" if note else ANALYTICS_NOTE
        unavailable = self._registry.unavailable_note()
        if unavailable is not None:
            note = f"{note}\n\n{unavailable}" if note else unavailable
        images: dict[str, bytes] = {}
        outcome = await self._agent.run(context, request.content, note, images)
        await self._save(turn, context, outcome, request, images)

    async def _staff(self, telegram_user_id: int) -> tuple[Staff | None, bool]:
        """The person of the turn, checked at the provider again. Second: whether the provider was away."""
        try:
            staff = await self._guard.check(telegram_user_id)
        except ProviderUnavailable:
            return None, True
        if staff is None or not staff.may_use:
            return None, False
        return staff, False

    async def _request_input(self, turn: CaseTurn, case: Case, pseudonymizer: Pseudonymizer) -> RequestInput:
        payload = turn.payload
        if turn.kind is TurnKind.FOLLOW_UP:
            return RequestInput([{"type": "text", "text": "Scheduled check of the case."}], {}, set(), "")

        lines = [
            f"From {payload['staff_account']}, a member of staff, in "
            + ("a private chat." if case.private else "a work group chat.")
        ]
        if payload["forwarded"]:
            lines.append("The message is forwarded from a client.")
        text = pseudonymizer.mask_text(payload["text"])
        lines.append(f"Message:\n{text}")
        quoted_text = ""
        if payload["quoted"] is not None:
            quoted_text = pseudonymizer.mask_text(payload["quoted"]["text"])
            lines.append(f"The message it replies to:\n{quoted_text or '(no text)'}")

        content: list[dict[str, Any]] = [{"type": "text", "text": "\n\n".join(lines)}]
        attachments: dict[str, Attachment] = {}
        raw = [*payload["attachments"], *((payload["quoted"] or {}).get("attachments") or [])]
        for item in raw:
            attachment = Attachment(**item)
            key = f"att_{len(attachments) + 1}"
            attachments[key] = attachment
            read = await self._reader.read(attachment)
            if read.unread_reason is not None:
                content.append(
                    {"type": "text", "text": f"Attachment {key} ({read.display_name}) could not be read: "
                     f"{read.unread_reason}."}
                )  # fmt: skip
                continue
            content.append({"type": "text", "text": f"Attachment {key} ({read.display_name}):"})
            content.extend(read.blocks)
        labels = {match.group() for match in LABEL.finditer(f"{text}\n{quoted_text}")}
        return RequestInput(content, attachments, labels, f"{text}\n{quoted_text}")

    async def _save(
        self,
        turn: CaseTurn,
        context: CaseContext,
        outcome: TurnOutcome,
        request: RequestInput,
        images: dict[str, bytes],
    ) -> None:
        case = context.case
        outbox = context.outbox
        async with self._database.unit_of_work() as session:
            stored = await session.get(Case, case.id, with_for_update=True)
            if stored is None:
                return
            stored.state = case.state
            stored.waiting_for = case.waiting_for
            stored.client_account = case.client_account
            stored.incident_id = case.incident_id
            stored.conversation = case.conversation
            stored.labels = context.labels.to_dict()
            stored.updated_at = context.now
            took_over = stored.owner_took_over and turn.kind is not TurnKind.OWNER_ANSWER

            done = await session.get(CaseTurn, turn.id)
            done.done_at = context.now
            journal = Journal(session)
            subject = context.staff.identity.subject
            if turn.kind in (TurnKind.REQUEST, TurnKind.RETRY, TurnKind.OWNER_MENTION):
                await journal.write(
                    JournalKind.REQUEST,
                    subject=subject,
                    telegram_user_id=context.staff.binding.telegram_user_id,
                    chat_id=case.chat_id,
                    case_id=case.id,
                    client_account=case.client_account,
                    escalated=context.escalated,
                )
            for call in context.gateway.calls:
                await journal.write(
                    JournalKind.SOURCE_CALL, subject=subject, case_id=case.id, source=call.source, tool=call.tool,
                    ok=call.ok,
                )  # fmt: skip
            await journal.write(
                JournalKind.USAGE,
                subject=subject,
                case_id=case.id,
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
                cache_read_tokens=outcome.cache_read_tokens,
                cache_write_tokens=outcome.cache_write_tokens,
            )

        if took_over:
            # The responsible person answered while the assistant worked: it writes no more there.
            outbox.replies.clear()
        elif turn.kind is TurnKind.OWNER_ANSWER:
            pass
        elif turn.kind is TurnKind.FOLLOW_UP:
            if case.state is not CaseState.WAITING_FIX and context.answer is not None:
                self._answer(outbox, turn, context, request, images, rated=False)
        elif context.answer is None:
            outbox.say(
                case.chat_id,
                self._words.say("busy_failed", reason=outcome.failure),
                reply_to=turn.payload["message_id"],
                thread_id=turn.payload["thread_id"],
                case_id=case.id,
            )
        else:
            self._answer(outbox, turn, context, request, images, rated=True)
        await self._postman.deliver(outbox)

    def _answer(
        self,
        outbox: Outbox,
        turn: CaseTurn,
        context: CaseContext,
        request: RequestInput,
        images: dict[str, bytes],
        *,
        rated: bool,
    ) -> None:
        case = context.case
        answer = context.answer
        assert answer is not None
        keyboard = None
        if rated:
            keyboard = [
                [
                    Button(self._words.say("rate_up"), f"f:{case.id}:up"),
                    Button(self._words.say("rate_down"), f"f:{case.id}:down"),
                ]
            ]
        placement = {"reply_to": turn.payload["message_id"], "thread_id": turn.payload["thread_id"]}
        outbox.add(
            Send(
                case.chat_id,
                answer.render(context.labels, self._words),
                keyboard=keyboard,
                case_id=case.id,
                role=MessageRole.ANSWER,
                **placement,
            )
        )
        for key in answer.attachments:
            if key in request.attachments:
                attachment = request.attachments[key]
                kind = "video" if attachment.kind in ("video", "animation") else "photo"
                if attachment.kind in ("photo", "video", "animation"):
                    outbox.add(SendMedia(case.chat_id, kind, attachment.file_id, key, case_id=case.id, **placement))
            elif key in images:
                outbox.add(SendMedia(case.chat_id, "photo", images[key], f"{key}.png", case_id=case.id, **placement))

    def _failed(self, turn: CaseTurn) -> Outbox:
        outbox = Outbox()
        if turn.kind not in (TurnKind.FOLLOW_UP, TurnKind.OWNER_ANSWER, TurnKind.OWNER_MENTION):
            outbox.say(
                turn.payload["chat_id"],
                self._words.say("busy_failed", reason="internal error"),
                reply_to=turn.payload["message_id"],
                thread_id=turn.payload["thread_id"],
            )
        return outbox

    async def _finish(self, turn: CaseTurn, outbox: Outbox) -> None:
        async with self._database.unit_of_work() as session:
            done = await session.get(CaseTurn, turn.id)
            if done is not None:
                done.done_at = datetime.datetime.now(datetime.UTC)
        await self._postman.deliver(outbox)


class FollowUpScheduler:
    """Puts a check into the queue of every case that waits for a fix, once per interval."""

    def __init__(self, database: Database, worker: CaseWorker, interval_seconds: int) -> None:
        self._database = database
        self._worker = worker
        self._interval = interval_seconds

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.tick()

    async def tick(self) -> int:
        queued = 0
        async with self._database.unit_of_work() as session:
            cases = CaseRepository(session)
            bindings = {
                binding.subject: binding for binding in await session.scalars(select(StaffBinding))
            }  # fmt: skip
            for case in await cases.waiting_for_fix():
                binding = bindings.get(case.staff_subject)
                if binding is None or await cases.has_pending_turn(case.id):
                    continue
                await cases.enqueue(
                    case,
                    TurnKind.FOLLOW_UP,
                    {
                        "staff_telegram_id": binding.telegram_user_id,
                        "chat_id": case.chat_id,
                        "message_id": case.root_message_id,
                        "thread_id": None,
                    },
                )
                queued += 1
        if queued:
            self._worker.wake.set()
        return queued
