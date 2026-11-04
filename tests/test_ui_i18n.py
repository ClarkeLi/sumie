"""The Studio page ships its own translation table; keep it well-formed."""
import json
import re
import unittest
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "src" / "DrawGuessBridge" / "ui.html"


def rows():
    text = UI.read_text(encoding="utf8")
    match = re.search(r"const I18N_ROWS = (\[.*?\]);\n", text, re.S)
    assert match, "I18N_ROWS not found in ui.html"
    return json.loads(match.group(1))


class UiI18nTest(unittest.TestCase):
    def test_rows_have_five_fields_and_no_empty_translation(self):
        for row in rows():
            self.assertEqual(len(row), 5, row[0])
            for cell in row:
                self.assertTrue(cell.strip(), row[0])

    def test_placeholders_match_across_languages(self):
        for row in rows():
            want = sorted(re.findall(r"\{\d\}", row[0]))
            for cell in row[1:]:
                self.assertEqual(sorted(re.findall(r"\{\d\}", cell)), want, row[0])

    def test_no_duplicate_source_strings(self):
        keys = [row[0] for row in rows()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_every_chinese_html_text_node_is_translated(self):
        html = UI.read_text(encoding="utf8")
        body = html.split("</style>", 1)[1].split("<script>", 1)[0]
        known = {row[0] for row in rows()}
        known_html = " ".join(known)
        missing = []
        for text in re.findall(r">([^<>]*[一-鿿][^<>]*)<", body):
            for part in text.strip().split(" · "):  # the page translates "a · b" per part
                if part and part not in known and part not in known_html:
                    missing.append(part)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
