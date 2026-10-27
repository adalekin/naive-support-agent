"""The assistant's own messages, in the language of the installation.

Answers about cases are written by the model in the language the instructions set; these are
the few fixed messages around them.
"""

from typing import Literal

Locale = Literal["ru", "en"]

WORDS: dict[str, dict[str, str]] = {
    "ru": {
        "login_link": "Чтобы пользоваться ассистентом, войдите через учётную запись организации:\n{link}\n"
        "Код подтверждения: {code}. Ссылка действует {minutes} мин.",
        "login_pending": "Ссылка для входа уже отправлена выше и действует ещё {minutes} мин.",
        "login_expired": "Код входа истёк, Telegram не привязан. Напишите ещё раз, пришлю новую ссылку.",
        "login_declined": "Вход отменён, Telegram не привязан. Напишите ещё раз, пришлю новую ссылку.",
        "login_no_role": "Вход выполнен, но у учётной записи {account} нет доступа к ассистенту. Telegram не привязан.",
        "bound": "Telegram привязан к учётной записи {account}. Присылайте кейсы.",
        "binding_notice": "Новая привязка: Telegram {telegram} привязан к учётной записи {account}.",
        "unbound_replaced": "Учётная запись {account} привязана к другому Telegram, этот отвязан.",
        "access_unavailable": "Не могу сейчас проверить доступ у провайдера входа, поэтому кейс не разбираю. "
        "Попробуйте позже.",
        "provider_unavailable": "Провайдер входа сейчас не отвечает, ссылку для входа выдать не могу. Попробуйте позже.",
        "taken": "Взялся за разбор.",
        "busy_failed": "Разбор не получился: {reason}. Кейс можно прислать ещё раз.",
        "rate_up": "Помогло",
        "rate_down": "Не помогло",
        "rated": "Спасибо, оценка записана.",
        "button_gone": "Эта кнопка больше не действует.",
        "not_yours": "Оценить ответ может тот, кто спросил.",
        "nothing_open": "Открытых кейсов нет.",
        "open_cases": "Открытые кейсы:",
        "case_closed": "Кейс закрыт.",
        "summary_owner_only": "Сводку видит только ответственный.",
        "attachment_unread": "Не смог прочитать вложение «{name}».",
        "part_client_saw": "Что видел клиент",
        "part_cause": "Причина",
        "part_whose_error": "Чья ошибка",
        "part_tell_client_now": "Что сказать клиенту сейчас",
        "part_next_steps": "Что дальше и кто",
        "escalation": "Нужен ответственный.",
        "escalation_complaint": "Жалоба",
        "escalation_checked": "Проверено",
        "escalation_found": "Найдено",
        "escalation_question": "Вопрос",
        "escalation_asked_by": "Спрашивал: {account}.",
        "nobody_to_call": "Позвать некого: ни у одного привязанного человека нет роли ответственного.",
        "rule_proposal": "Предлагаю правило для инструкций, чтобы такой кейс решался без вас:",
        "what_is_wrong": "Что не так с ответом? Напишите ответом на это сообщение, разберу заново.",
        "fix_available": "Исправление доступно клиентам, клиенту можно сообщить.",
    },
    "en": {
        "login_link": "To use the assistant, sign in with your organization account:\n{link}\n"
        "Confirmation code: {code}. The link works for {minutes} min.",
        "login_pending": "The sign-in link is above and works for {minutes} more min.",
        "login_expired": "The sign-in code expired; Telegram is not bound. Write again for a new link.",
        "login_declined": "The sign-in was declined; Telegram is not bound. Write again for a new link.",
        "login_no_role": "You signed in, but the account {account} has no access to the assistant. Telegram is not bound.",
        "bound": "Telegram is bound to the account {account}. Send your cases.",
        "binding_notice": "New binding: Telegram {telegram} is bound to the account {account}.",
        "unbound_replaced": "The account {account} is now bound to another Telegram; this one is unbound.",
        "access_unavailable": "I cannot check your access at the sign-in provider now, so I am not looking into the "
        "case. Try again later.",
        "provider_unavailable": "The sign-in provider does not answer, so I cannot give you a sign-in link. "
        "Try again later.",
        "taken": "On it.",
        "busy_failed": "The investigation failed: {reason}. You can send the case again.",
        "rate_up": "Helped",
        "rate_down": "Did not help",
        "rated": "Thanks, the rating is saved.",
        "button_gone": "This button no longer works.",
        "not_yours": "Only the person who asked can rate the answer.",
        "nothing_open": "No open cases.",
        "open_cases": "Open cases:",
        "case_closed": "The case is closed.",
        "summary_owner_only": "Only a responsible person sees the summary.",
        "attachment_unread": "Could not read the attachment “{name}”.",
        "part_client_saw": "What the client saw",
        "part_cause": "Cause",
        "part_whose_error": "Whose error",
        "part_tell_client_now": "What to tell the client now",
        "part_next_steps": "What happens next and who does it",
        "escalation": "A responsible person is needed.",
        "escalation_complaint": "Complaint",
        "escalation_checked": "Checked",
        "escalation_found": "Found",
        "escalation_question": "Question",
        "escalation_asked_by": "Asked by {account}.",
        "nobody_to_call": "There is nobody to call: no bound person has the role of a responsible person.",
        "rule_proposal": "A rule for the instructions, so that such a case is solved without you:",
        "what_is_wrong": "What is wrong with the answer? Reply to this message and I will look again.",
        "fix_available": "The fix is available to clients; the client can be told.",
    },
}


class Words:
    def __init__(self, locale: Locale) -> None:
        self._words = WORDS[locale]

    def say(self, key: str, **values: object) -> str:
        return self._words[key].format(**values)
