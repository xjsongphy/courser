from courser.editing import FieldEditor
from textual.events import Key


def test_field_editor_accepts_named_printable_symbols() -> None:
    editor = FieldEditor()
    editor.begin("")

    # Textual represents some printable keys with a named key (for example
    # ``at``), while the actual typed character is in ``character``.
    for key, character in (("at", "@"), ("full_stop", "."),
                           ("hyphen", "-"), ("underscore", "_")):
        event = Key(key, character)
        editor.feed(event.key, event.character, event.is_printable)

    assert editor.text == "@.-_"


def test_textual_single_character_keys_still_work() -> None:
    editor = FieldEditor()
    editor.begin("")

    event = Key("x", None)
    editor.feed(event.key, event.character, event.is_printable)

    assert editor.text == "x"
