import os
import re
import unittest
from pathlib import Path

from emx import cli, sidebar

DOCS = Path(__file__).resolve().parent.parent / "user-docs"
TEST_ONLY_SETTINGS = {"EMX_SHOW_TEST_SESSIONS", "EMX_CONFIG_HOME"}


def read(name):
    return (DOCS / name).read_text(encoding="utf-8")


def split_top(text, sep):
    """Split on `sep` at bracket depth 0."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif depth == 0 and text.startswith(sep, i):
            parts.append(text[start:i])
            start = i + len(sep)
    parts.append(text[start:])
    return parts


def public_commands():
    first = cli.USAGE.splitlines()[0]
    inner = first[first.index("[") + 1:first.rindex("]")]
    found = set()
    for alternative in split_top(inner, " | "):
        word = alternative.split()[0]
        found.add(f"emx {word}")
        nested = re.search(r"\[([^\]]*)\]", alternative)
        if nested and " | " in nested.group(1):
            for option in nested.group(1).split(" | "):
                found.add(f"emx {word} {option}")
    return found


def panel_key_tokens():
    return {token for keys, _ in sidebar.HELP + sidebar.NARROW_HELP if keys for token in keys.split()}


def root_keys():
    return {"^" + key[2:] for key in (sidebar.BACK_KEY, sidebar.COLLAPSE_KEY, sidebar.PASTE_KEY)} | {"←"}


def public_settings():
    return set(re.findall(r"EMX_[A-Z_]+", cli.USAGE)) - TEST_ONLY_SETTINGS


class DocsMatchEmx(unittest.TestCase):
    def assert_in_reference(self, required, where):
        self.assertTrue(f"`{required}`" in read("reference.md"), f'"{required}" is in {where} but not in user-docs/reference.md')

    def test_every_public_command_is_in_the_reference(self):
        commands = public_commands()
        self.assertIn("emx docs", commands)
        self.assertIn("emx update off", commands)
        for command in sorted(commands):
            with self.subTest(command=command):
                self.assert_in_reference(command, "emx --help")

    def test_no_internal_command_is_parsed_as_public(self):
        internal = {f"emx {w}" for w in ("hook", "install", "relaunch", "follow", "open", "sidebar", "left", "blank", "paste", "paste-image")}
        self.assertEqual(public_commands() & internal, set())

    def test_every_panel_key_is_in_the_reference(self):
        tokens = panel_key_tokens()
        self.assertTrue(tokens)
        for token in sorted(tokens):
            with self.subTest(key=token):
                self.assert_in_reference(token, "the panel's key help")

    def test_the_root_keys_are_in_the_reference(self):
        for key in sorted(root_keys()):
            with self.subTest(key=key):
                self.assert_in_reference(key, "emx's root key bindings")

    def test_every_public_setting_is_in_the_reference(self):
        settings = public_settings()
        self.assertTrue(settings)
        for name in sorted(settings):
            with self.subTest(setting=name):
                self.assert_in_reference(name, "emx --help")

    def test_no_test_only_setting_is_in_the_docs(self):
        for page in sorted(DOCS.glob("*.md")):
            for name in sorted(TEST_ONLY_SETTINGS):
                with self.subTest(page=page.name, setting=name):
                    self.assertNotIn(name, page.read_text(encoding="utf-8"))

    def test_the_index_links_every_page(self):
        index = read("index.md")
        for page in sorted(DOCS.glob("*.md")):
            if page.name == "index.md":
                continue
            with self.subTest(page=page.stem):
                self.assertIn(f"({page.name})", index)
                self.assertIn(f"[{page.stem}]", index)

    def test_the_index_has_the_four_sections(self):
        index = read("index.md")
        for heading in ("Getting started", "Guides", "Concepts", "Reference"):
            with self.subTest(heading=heading):
                self.assertIn(f"## {heading}", index)


if __name__ == "__main__":
    unittest.main()
