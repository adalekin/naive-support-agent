from pathlib import Path

import pytest

from internal.config import ConfigurationError, InstructionSet

SOURCES = """
personal_fields = ["email", "full_name"]

[[source]]
kind = "mcp"
name = "database"
description = "The product database, read only."
transport = "stdio"
command = ["db-mcp"]
env = ["DATABASE_URI"]
tools = ["execute_sql"]
"""


def write(directory: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")
    return directory


def test_instructions_are_loaded_in_name_order(tmp_path):
    write(tmp_path, {"20-tone.md": "Write briefly.", "10-cases.md": "Look at payments first.", "sources.toml": SOURCES})
    instructions = InstructionSet.load(tmp_path)
    assert instructions.text == "Look at payments first.\n\nWrite briefly."
    assert [spec.name for spec in instructions.sources] == ["database"]
    assert instructions.personal_fields == {"email", "full_name"}


def test_no_instructions_no_start(tmp_path):
    write(tmp_path, {"sources.toml": SOURCES, "empty.md": "  \n"})
    with pytest.raises(ConfigurationError, match="no instructions"):
        InstructionSet.load(tmp_path)


def test_no_directory_no_start(tmp_path):
    with pytest.raises(ConfigurationError, match="not a directory"):
        InstructionSet.load(tmp_path / "missing")


def test_changed_instructions_are_read_on_the_next_start(tmp_path):
    write(tmp_path, {"cases.md": "Old rule.", "sources.toml": SOURCES})
    assert InstructionSet.load(tmp_path).text == "Old rule."
    write(tmp_path, {"cases.md": "New rule."})
    assert InstructionSet.load(tmp_path).text == "New rule."


def test_a_source_without_its_list_of_tools_is_refused(tmp_path):
    write(tmp_path, {"cases.md": "x", "sources.toml": SOURCES.replace('tools = ["execute_sql"]', "")})
    with pytest.raises(ConfigurationError, match="tools"):
        InstructionSet.load(tmp_path)


def test_interface_source_needs_the_product_address(tmp_path):
    sources = SOURCES.replace("tools = [", "interface = true\ntools = [")
    write(tmp_path, {"cases.md": "x", "sources.toml": sources})
    with pytest.raises(ConfigurationError, match="product_url"):
        InstructionSet.load(tmp_path)
    write(
        tmp_path,
        {
            "sources.toml": sources.replace(
                "interface = true", 'interface = true\nproduct_url = "https://app.example.com"'
            )
        },
    )
    assert InstructionSet.load(tmp_path).has_interface


def test_without_interface_source_the_assistant_still_starts(tmp_path):
    write(tmp_path, {"cases.md": "x", "sources.toml": SOURCES})
    assert not InstructionSet.load(tmp_path).has_interface
