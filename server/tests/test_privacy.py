import json

from internal.privacy.pseudonymizer import LabelMap, Pseudonymizer


def pseudonymizer(labels=None):
    return Pseudonymizer(frozenset({"full_name", "card_last4", "email"}), LabelMap(labels))


def test_email_in_the_case_reaches_the_model_as_a_label():
    masked = pseudonymizer().mask_text("client@example.com говорит, что деньги списались")
    assert "client@example.com" not in masked
    assert masked.startswith("<<email_1>>")


def test_the_same_value_keeps_its_label_across_messages_of_a_case():
    first = pseudonymizer()
    first.mask_text("client@example.com")
    again = pseudonymizer(first.labels.to_dict())
    assert again.mask_text("пишет client@example.com снова") == "пишет <<email_1>> снова"


def test_phones_in_several_shapes():
    masked = pseudonymizer().mask_text("+7 912 345-67-89, 8 (912) 345-67-89 и 79123456789; заказ 12345")
    assert "912" not in masked
    assert "12345" in masked


def test_name_inside_free_text_reaches_the_model_as_is():
    assert pseudonymizer().mask_text("Меня зовут Иван Петров") == "Меня зовут Иван Петров"


def test_names_and_payment_data_are_masked_by_the_source_fields():
    rows = [{"id": 7, "full_name": "Иван Петров", "card_last4": "4242", "amount": 990, "email": "a@b.co"}]
    masked = json.loads(pseudonymizer().mask_source_text(str(rows)))
    assert masked[0]["id"] == 7 and masked[0]["amount"] == 990
    assert masked[0]["full_name"].startswith("<<pd_")
    assert "4242" not in json.dumps(masked)
    assert "a@b.co" not in json.dumps(masked)


def test_arguments_of_a_call_get_the_values_back():
    p = pseudonymizer()
    label = p.mask_text("client@example.com")
    assert p.unmask({"sql": f"select * from users where email = '{label}'"}) == {
        "sql": "select * from users where email = 'client@example.com'"
    }


def test_text_that_is_not_a_structure_is_masked_as_text():
    assert pseudonymizer().mask_source_text("[error] user a@b.co not found") == "[error] user <<email_1>> not found"


def test_client_email_in_an_answer_is_under_a_spoiler():
    from internal.privacy.render import TextBuilder
    from internal.telegram import Entity

    labels = LabelMap()
    label = labels.label("email", "client@example.com")
    text = (
        TextBuilder(labels)
        .styled("bold", "Причина 🙂")
        .plain("\n")
        .revealed(f"У {label} списание без результата")
        .build()
    )
    assert text.text == "Причина 🙂\nУ client@example.com списание без результата"
    assert text.entities == (Entity("bold", 0, 10), Entity("spoiler", 13, 18))


def test_sum_without_personal_data_is_not_hidden():
    from internal.privacy.render import TextBuilder

    text = TextBuilder(LabelMap()).revealed("Списано 990 ₽ один раз").build()
    assert text.entities == ()


def test_a_label_the_case_does_not_know_stays_as_written():
    from internal.privacy.render import TextBuilder

    assert TextBuilder(LabelMap()).revealed("<<email_9>>").build().text == "<<email_9>>"
