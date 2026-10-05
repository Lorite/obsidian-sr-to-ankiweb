"""Parser and conversion tests. They need no Anki collection and no network."""

import sr_anki_sync as s

ST = s.SRSettings(cloze_patterns=["==[123;;]answer[;;hint]=="])
CLOZE_RES = [s.cloze_regex(p) for p in ST.cloze_patterns]


def parse(text):
    """The cards without their line numbers."""
    return [(t, raw) for t, raw, _ in s.parse(text, ST, CLOZE_RES)]


def test_single_line_cards():
    cards = parse("Capital of Denmark :: Copenhagen\n\nhej ::: hello")
    assert cards == [(s.SINGLE, "Capital of Denmark :: Copenhagen"), (s.SINGLE_REV, "hej ::: hello")]
    assert s.split_front_back(*cards[0], ST) == ("Capital of Denmark ", " Copenhagen")


def test_separator_inside_inline_code_is_ignored():
    assert parse("Use `a::b` in C++ for scope") == []


def test_multi_line_cards():
    text = "What are the\nthree laws?\n?\nFirst\nSecond\nThird\n\nNext paragraph"
    cards = parse(text)
    assert cards == [(s.MULTI, "What are the\nthree laws?\n?\nFirst\nSecond\nThird")]
    assert s.split_front_back(*cards[0], ST) == ("What are the\nthree laws?", "First\nSecond\nThird")
    reversed_cards = parse("front\n??\nback")
    assert reversed_cards == [(s.MULTI_REV, "front\n??\nback")]


def test_cloze_cards_and_conversion():
    cards = parse("The ==Øresund== bridge links ==Denmark== and Sweden.")
    assert [c[0] for c in cards] == [s.CLOZE]
    converted = s.to_anki_cloze(cards[0][1], CLOZE_RES)
    assert converted == "The {{c1::Øresund}} bridge links {{c2::Denmark}} and Sweden."


def test_numbered_cloze_with_hint():
    assert s.to_anki_cloze("==2;;foo;;bar== and ==z==", CLOZE_RES) == "{{c2::foo::bar}} and {{c1::z}}"


def test_equality_operators_are_not_clozes():
    assert parse("if a == b and c == d") == []


def test_code_block_does_not_start_cards():
    assert parse("```\nkey :: value\n```") == []


def test_dataview_fields_are_filtered():
    assert s.DATAVIEW_FIELD_RE.search("- [ ] #habit Read (reading:: 30) minutes")
    assert s.DATAVIEW_FIELD_RE.search("status:: done")
    assert not s.DATAVIEW_FIELD_RE.search("What is entropy? :: A measure of disorder")


def test_guids_are_stable_and_distinct():
    a = s.Card("basic", "Q", "A1", "x/one.md", "Obsidian::x", [])
    b = s.Card("basic", "Q", "A2", "x/two.md", "Obsidian::x", [])
    c = s.Card("basic", "Q", "A3", "y/one.md", "Obsidian::y", [])
    s.assign_guids([a, b])
    first = (a.guid, b.guid)
    s.assign_guids([c])
    assert first[0] != first[1]
    assert c.guid == first[0]  # moving a note keeps the id of its first occurrence


def test_noise_is_stripped():
    assert s.strip_noise("Q :: A <!--SR:!2026-01-01,3,250--> ^sr-id-abc") == "Q :: A "


def test_tag_decks_when_folders_are_off(tmp_path, monkeypatch):
    plugin = tmp_path / ".obsidian/plugins/obsidian-spaced-repetition"
    plugin.mkdir(parents=True)
    (plugin / "data.json").write_text('{"settings": {"convertFoldersToDecks": false, "flashcardTags": ["#flashcards"]}}')
    (tmp_path / "a").mkdir()
    (tmp_path / "a/tagged.md").write_text("---\ntags: [flashcards/spanish]\n---\nhola :: hello\n")
    (tmp_path / "a/inline.md").write_text("#flashcards\n\nQ :: A\n")
    (tmp_path / "a/untagged.md").write_text("ignored :: card\n")
    monkeypatch.setattr(s, "VAULT", tmp_path)
    monkeypatch.setattr(s, "SR_DATA", plugin / "data.json")
    cards = s.collect(s.SRSettings.load())
    assert sorted((c.path, c.deck) for c in cards) == [
        ("a/inline.md", "Obsidian::flashcards"),
        ("a/tagged.md", "Obsidian::flashcards::spanish"),
    ]


def test_card_lines_and_heading_context():
    text = "# Title\n\n## Part A\n\nQ1 :: A1\n\n### Detail\n\nfront\n?\nback\n\n## Part B\n\n==cloze== here"
    cards = s.parse(text, ST, CLOZE_RES)
    assert [line for _, _, line in cards] == [4, 8, 14]
    heads = s.headings_of(text)
    assert [s.question_context(heads, line) for _, _, line in cards] == [
        ["Title", "Part A"],
        ["Title", "Part A", "Detail"],
        ["Title", "Part B"],
    ]


def test_headings_in_code_and_links_are_cleaned():
    text = "```\n# not a heading\n```\n## See [[Other note|other]] [^1] [x](#x)\nQ :: A"
    assert s.question_context(s.headings_of(text), 4) == ["See other x"]


def test_context_skip(monkeypatch):
    monkeypatch.setattr(s, "CONTEXT_SKIP", s.re.compile(r"Flashcards|AI Generated.*", s.re.I))
    text = "# AI Generated\n## Flashcards\n### Kinematics\nQ :: A"
    assert s.question_context(s.headings_of(text), 3) == ["Kinematics"]


def test_heading_equal_to_title_is_dropped(tmp_path, monkeypatch):
    plugin = tmp_path / ".obsidian/plugins/obsidian-spaced-repetition"
    plugin.mkdir(parents=True)
    (plugin / "data.json").write_text('{"settings": {"convertFoldersToDecks": true}}')
    (tmp_path / "Danish.md").write_text("# Danish\n## Cards\nhej ::: hello\n")
    monkeypatch.setattr(s, "VAULT", tmp_path)
    monkeypatch.setattr(s, "SR_DATA", plugin / "data.json")
    assert [c.context for c in s.collect(s.SRSettings.load())] == [["Danish", "Cards"]]
