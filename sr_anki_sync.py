#!/usr/bin/env python3
"""One-way sync of the Obsidian spaced-repetition flashcards to Anki and AnkiWeb.

The vault is the source of truth for card CONTENT. Anki owns the SCHEDULING: reviews made
in AnkiDroid, AnkiWeb or Anki desktop are pulled in before every push, and an edited card
keeps its review history as long as its question text is unchanged.

What counts as a card follows the obsidian-spaced-repetition plugin itself: its parser
(src/parser.ts) is ported line for line, and its separators, cloze patterns and ignored
folders are read from the vault's own .obsidian/plugins/obsidian-spaced-repetition/
data.json. With convertFoldersToDecks the folder is the deck, so a note at
work/concepts/x.md lands in the Anki deck Obsidian::work::concepts.

Only notes of the three "Obsidian SR" note types are ever created, changed or deleted.
Any other deck or note in the AnkiWeb collection is left alone.

Commands:
  login    store an AnkiWeb sync key (asks for the password, never stores it)
  stats    parse the vault and print card counts per deck, no Anki access
  sync     pull from AnkiWeb, apply the vault, push to AnkiWeb (--dry-run, --offline)

https://github.com/Lorite/obsidian-sr-to-ankiweb (MIT). The parser is a port of
obsidian-spaced-repetition, Copyright (c) 2021 - 2024 Stephen Mwangi, MIT License.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import io
import getpass
import hashlib
import html
import json
import os
import re
import sys
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

# The vault to read. Also settable with --vault.
VAULT = Path(os.environ.get("SR_ANKI_VAULT", "")).expanduser()
HOME_DIR = Path(os.environ.get("SR_ANKI_HOME", Path.home() / ".local/share/sr-anki-sync")).expanduser()
AUTH_FILE = Path(os.environ.get("SR_ANKI_AUTH", Path.home() / ".config/sr-anki-sync/ankiweb-auth.json")).expanduser()
# The vault name in obsidian:// links on the card back (the folder name by default).
VAULT_NAME = os.environ.get("SR_ANKI_VAULT_NAME", VAULT.name)
ROOT_DECK = os.environ.get("SR_ANKI_ROOT_DECK", "Obsidian")
SR_DATA = VAULT / ".obsidian/plugins/obsidian-spaced-repetition/data.json"
# Extra vault path prefixes to leave out of Anki, comma-separated (e.g. "ai_chats/,tests/").
EXCLUDE = [p.strip() for p in os.environ.get("SR_ANKI_EXCLUDE", "").split(",") if p.strip()]
STATE_FILE = HOME_DIR / "state.json"


def env_int(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    return int(value) if value else None


# Deck options for every deck under ROOT_DECK, kept in one Anki preset named after it.
# Unset means "do not touch", so the options stay whatever was chosen in Anki.
NEW_PER_DAY = env_int("SR_ANKI_NEW_PER_DAY")
REVIEWS_PER_DAY = env_int("SR_ANKI_REVIEWS_PER_DAY")
# Order of unseen cards: "newest" (newest note first, by its created date), "oldest", or
# empty to keep the order in which the cards were added.
NEW_ORDER = os.environ.get("SR_ANKI_NEW_ORDER", "").strip().lower()
# Headings to leave out of the breadcrumb above each card, as one regex matched against the
# whole heading, case-insensitive. Useful for template headings such as "Flashcards".
CONTEXT_SKIP = re.compile(os.environ.get("SR_ANKI_CONTEXT_SKIP", "") or r"(?!)", re.I)
# One deck per note, nested under its folder (or tag) deck: Obsidian::work::concepts::Note.
# Studying the folder deck still includes all its notes, because Anki studies a parent deck
# together with its subdecks.
NOTE_DECKS = os.environ.get("SR_ANKI_NOTE_DECKS", "").strip().lower() in ("1", "true", "yes", "on")
# Review statistics: one CSV per calendar day with reviews, in <dir>/<YYYY-MM-DD>/
# Anki_Reviews_<YYYY-MM-DD>.csv. A relative dir is relative to the vault. Every sync pulls
# from AnkiWeb when this is set, also when the vault is unchanged, so the counts stay
# current. The last STATS_DAYS days are rewritten (reviews made offline arrive late).
STATS_DIR = os.environ.get("SR_ANKI_STATS_DIR", "").strip()
STATS_DAYS = env_int("SR_ANKI_STATS_DAYS") or 7
# Frontmatter keys that hold a note's creation date, first match wins. File mtime otherwise.
CREATED_KEYS = ("created", "date_created", "date")
# Never parsed, whatever the plugin settings say.
ALWAYS_SKIP_DIRS = {".git", ".obsidian", ".trash", ".stfolder", "node_modules"}

MODEL_BASIC = "Obsidian SR Basic"
MODEL_REVERSED = "Obsidian SR Reversed"
MODEL_CLOZE = "Obsidian SR Cloze"
GUID_PREFIX = "osr-"

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"}
# Media larger than this stays out of Anki (AnkiWeb rejects very large files).
MAX_MEDIA_BYTES = 5 * 1024 * 1024


# --------------------------------------------------------------------------- settings


@dataclass
class SRSettings:
    single: str = "::"
    single_rev: str = ":::"
    multi: str = "?"
    multi_rev: str = "??"
    multi_end: str = ""
    cloze_patterns: list[str] = field(default_factory=list)
    folders_to_ignore: list[str] = field(default_factory=list)
    note_tags_to_ignore: list[str] = field(default_factory=list)
    convert_folders: bool = True
    show_context: bool = True
    flashcard_tags: list[str] = field(default_factory=lambda: ["#flashcards"])

    @classmethod
    def load(cls) -> "SRSettings":
        s = json.loads(SR_DATA.read_text())["settings"]
        patterns = list(s.get("clozePatterns", []))
        # The plugin derives the active patterns from these three switches.
        toggles = {
            "convertHighlightsToClozes": "==[123;;]answer[;;hint]==",
            "convertBoldTextToClozes": "**[123;;]answer[;;hint]**",
            "convertCurlyBracketsToClozes": "{{[123;;]answer[;;hint]}}",
        }
        for key, pat in toggles.items():
            if s.get(key) and pat not in patterns:
                patterns.append(pat)
            if not s.get(key) and pat in patterns:
                patterns.remove(pat)
        return cls(
            single=s.get("singleLineCardSeparator", "::"),
            single_rev=s.get("singleLineReversedCardSeparator", ":::"),
            multi=s.get("multilineCardSeparator", "?"),
            multi_rev=s.get("multilineReversedCardSeparator", "??"),
            multi_end=s.get("multilineCardEndMarker", ""),
            cloze_patterns=patterns,
            folders_to_ignore=list(s.get("noteFoldersToIgnore", [])),
            note_tags_to_ignore=list(s.get("noteTagsToIgnore", [])),
            convert_folders=bool(s.get("convertFoldersToDecks", False)),
            show_context=bool(s.get("showContextInCards", True)),
            flashcard_tags=list(s.get("flashcardTags", ["#flashcards"])),
        )


# Stricter than clozecraft's (.+?): no space just inside the markers, as for an Obsidian
# highlight. Without it, "if a == b and c == d" in code or logs became a cloze card.
ANSWER_RE = r"(?P<answer>\S(?:.*?\S)??)"
# Dataview inline fields, "(habit:: true)" or "status:: done". The plugin reads them as
# cards, which fills daily notes with junk cards.
DATAVIEW_FIELD_RE = re.compile(r"[\[(]\s*[A-Za-z_][\w -]*::\s|^\s*(?:[-*]\s+)?[A-Za-z_][\w-]*::\s")


def cloze_regex(pattern: str) -> re.Pattern:
    """Turn a clozecraft pattern such as ==[123;;]answer[;;hint]== into a regex.

    Groups: seq (optional), answer, hint (optional).
    """
    num = re.search(r"\[[^\]]*\d+[^\]]*\]", pattern)
    hint = re.search(r"\[[^\]]*hint[^\]]*\]", pattern)
    out, pos = "", 0
    for m, kind in sorted(((num, "num"), (hint, "hint")), key=lambda x: x[0].start()):
        chunk = pattern[pos:m.start()]
        out += re.escape(chunk).replace("answer", ANSWER_RE)
        inner = re.escape(m.group(0)[1:-1])
        if kind == "num":
            out += "(?:" + re.sub(r"\d+", "(?P<seq>\\\\d+)", inner) + ")?"
        else:
            out += "(?:" + inner.replace("hint", "(?P<hint>.+?)") + ")?"
        pos = m.end()
    out += re.escape(pattern[pos:]).replace("answer", ANSWER_RE)
    return re.compile(out)


# --------------------------------------------------------------------------- parser
# A port of obsidian-spaced-repetition src/parser.ts (upstream main, 2026-08-03).

SINGLE, SINGLE_REV, MULTI, MULTI_REV, CLOZE = "single", "single_rev", "multi", "multi_rev", "cloze"


def marker_inside_code(text: str, marker: str, idx: int) -> bool:
    before = text[:idx].count("`")
    after = text[idx + len(marker):].count("`")
    return before % 2 == 1 and after % 2 == 1


def has_inline_marker(text: str, marker: str) -> bool:
    if not marker:
        return False
    idx = text.find(marker)
    return idx != -1 and not marker_inside_code(text, marker, idx)


def parse(text: str, st: SRSettings, cloze_res: list[re.Pattern]) -> list[tuple[str, str, int]]:
    """Return (card type, raw card text, first line) for each card, exactly as the plugin
    finds them. Lines count from 0."""
    inline = sorted([(st.single, SINGLE), (st.single_rev, SINGLE_REV)], key=lambda x: -len(x[0]))
    cards: list[tuple[str, str, int]] = []
    card_text, card_type, first = "", None, 0
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line, trimmed = lines[i], lines[i].strip()
        if line.startswith("<!--") and not line.startswith("<!--SR:"):
            # Same quirk as the plugin: it tests the FIRST line for "-->", so a multi-line
            # comment swallows the rest of the note.
            while i + 1 < len(lines) and "-->" not in line:
                i += 1
            i += 2
            continue
        empty = len(trimmed) == 0
        end_marker = bool(st.multi_end) and trimmed == st.multi_end
        if (empty and not st.multi_end) or (empty and card_type is None) or end_marker:
            if card_type:
                cards.append((card_type, card_text.rstrip(), first))
                card_type = None
            card_text = ""
            i += 1
            continue
        if not card_text:
            first = i
        if card_text:
            card_text += "\n"
        card_text += line.rstrip()
        for sep, typ in inline:
            if has_inline_marker(line, sep):
                card_type = typ
                break
        if card_type in (SINGLE, SINGLE_REV):
            card_text, first = line, i
            if i + 1 < len(lines) and lines[i + 1].startswith("<!--SR:"):
                i += 1
            cards.append((card_type, card_text, first))
            card_type, card_text = None, ""
        elif trimmed == st.multi:
            if len(card_text) > 1:
                card_type = MULTI
        elif trimmed == st.multi_rev:
            if len(card_text) > 1:
                card_type = MULTI_REV
        elif line.startswith("```") or line.startswith("~~~"):
            fence = re.match(r"`+|~+", line).group(0)
            while i + 1 < len(lines) and not lines[i + 1].startswith(fence):
                i += 1
                card_text += "\n" + lines[i]
            card_text += "\n" + fence
            i += 1
        elif card_type is None and any(r.search(line) for r in cloze_res):
            card_type = CLOZE
        i += 1
    if card_type and card_text:
        cards.append((card_type, card_text.rstrip(), first))
    return cards


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def headings_of(text: str) -> list[tuple[int, int, str]]:
    """(line, level, text) of every Markdown heading outside fenced code."""
    out, fence = [], None
    for n, line in enumerate(text.split("\n")):
        m = re.match(r"\s*(`{3,}|~{3,})", line)
        if m:
            fence = None if fence and m.group(1).startswith(fence) else (fence or m.group(1))
            continue
        if fence is None:
            h = HEADING_RE.match(line)
            if h:
                out.append((n, len(h.group(1)), h.group(2)))
    return out


def question_context(headings: list[tuple[int, int, str]], card_line: int) -> list[str]:
    """The heading path above a card, as the plugin's getQuestionContext builds it."""
    stack: list[tuple[int, str]] = []
    for line, level, title in headings:
        if line > card_line:
            break
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
    out = []
    for _, title in stack:
        title = re.sub(r"\[\^\d+\]", "", title)
        title = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", title)
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title)
        title = re.sub(r"\s+", " ", re.sub(r"(==|\*\*|__)", "", title)).strip()
        if title and not CONTEXT_SKIP.fullmatch(title):
            out.append(title)
    return out


def split_front_back(typ: str, text: str, st: SRSettings) -> tuple[str, str]:
    if typ in (SINGLE, SINGLE_REV):
        sep = st.single if typ == SINGLE else st.single_rev
        idx = text.find(sep)
        return text[:idx], text[idx + len(sep):]
    if typ in (MULTI, MULTI_REV):
        sep = st.multi if typ == MULTI else st.multi_rev
        lines = text.split("\n")
        idx = next(n for n, ln in enumerate(lines) if ln.strip() == sep)
        return "\n".join(lines[:idx]), "\n".join(lines[idx + 1:])
    return text, ""


# --------------------------------------------------------------------------- vault walk

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)


def is_ignored(rel: str, st: SRSettings) -> bool:
    for pat in st.folders_to_ignore:
        if pat.endswith("/"):
            if rel.startswith(pat) or ("/" + pat) in ("/" + rel):
                return True
        elif fnmatch(rel, pat) or fnmatch(rel, pat.lstrip("*/")):
            return True
    return False


def read_frontmatter(frontmatter: str) -> dict:
    try:
        import yaml

        data = yaml.safe_load(frontmatter) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def note_tags(data: dict) -> list[str]:
    tags = data.get("tags")
    if isinstance(tags, str):
        tags = re.split(r"[,\s]+", tags)
    return [str(t).lstrip("#") for t in (tags or []) if t]


INLINE_TAG_RE = re.compile(r"(?<![\w#&])#([A-Za-z_][\w/-]*)")


def tag_deck(tags: list[str], st: SRSettings) -> list[str] | None:
    """Without folder decks the plugin only reads notes that carry one of its flashcard
    tags (or a child, e.g. #flashcards/spanish), and that tag is the deck."""
    for wanted in st.flashcard_tags:
        root = [p for p in wanted.lstrip("#").split("/") if p]
        for tag in tags:
            path = [p for p in tag.lstrip("#").split("/") if p]
            if path[: len(root)] == root:
                return path
    return None


def note_created(data: dict, path: Path) -> float:
    """The note's creation time as a timestamp, from the frontmatter or the file mtime."""
    import datetime as dt

    for key in CREATED_KEYS:
        value = data.get(key)
        if isinstance(value, dt.datetime):
            return value.replace(tzinfo=value.tzinfo or dt.timezone.utc).timestamp()
        if isinstance(value, dt.date):
            return dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc).timestamp()
        if isinstance(value, str):
            try:
                parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                continue
            return parsed.replace(tzinfo=parsed.tzinfo or dt.timezone.utc).timestamp()
    return path.stat().st_mtime


@dataclass
class Card:
    kind: str  # basic | reversed | cloze
    front: str  # markdown (cloze: the whole text)
    back: str
    path: str  # vault-relative note path
    deck: str
    tags: list[str]
    created: float = 0.0
    context: list[str] = field(default_factory=list)
    guid: str = ""


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def collect(st: SRSettings) -> list[Card]:
    cloze_res = [cloze_regex(p) for p in st.cloze_patterns]
    cards: list[Card] = []
    for dirpath, dirnames, filenames in os.walk(VAULT):
        dirnames[:] = sorted(d for d in dirnames if d not in ALWAYS_SKIP_DIRS)
        for fn in sorted(filenames):
            if not fn.endswith(".md"):
                continue
            path = Path(dirpath) / fn
            rel = path.relative_to(VAULT).as_posix()
            if is_ignored(rel, st) or any(rel.startswith(p) for p in EXCLUDE):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            fm = FRONTMATTER_RE.match(text)
            meta = read_frontmatter(fm.group(1)) if fm else {}
            tags = note_tags(meta)
            created = note_created(meta, path)
            if any(t.lstrip("#") in tags for t in st.note_tags_to_ignore):
                continue
            # The plugin blanks the frontmatter but keeps its lines, so do the same.
            if fm:
                text = "\n" * fm.group(0).count("\n") + text[fm.end():]
            if st.convert_folders:
                folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
                deck_path = [p for p in folder.split("/") if p]
            else:
                body = re.sub(r"```.*?```", "", text, flags=re.S)
                deck_path = tag_deck(tags + INLINE_TAG_RE.findall(body), st)
                if deck_path is None:
                    continue
            title = Path(rel).stem
            if NOTE_DECKS:
                deck_path = deck_path + [deck_title(title)]
            deck = "::".join([ROOT_DECK] + deck_path)
            heads = headings_of(text) if st.show_context else []
            for typ, raw, line_no in parse(text, st, cloze_res):
                if typ in (SINGLE, SINGLE_REV) and DATAVIEW_FIELD_RE.search(raw):
                    continue
                front, back = split_front_back(typ, raw, st)
                kind = {SINGLE: "basic", MULTI: "basic", SINGLE_REV: "reversed", MULTI_REV: "reversed"}.get(
                    typ, "cloze"
                )
                if kind != "cloze" and not norm(strip_noise(front)):
                    continue
                # A heading that repeats the note title (a common "# Title" line) adds nothing.
                context = [title] + [h for h in question_context(heads, line_no) if h.casefold() != title.casefold()]
                cards.append(Card(kind, front, back, rel, deck, tags, created, context))
    assign_guids(cards)
    return cards


def deck_title(title: str) -> str:
    """A note title as one deck name component ("::" separates Anki decks)."""
    return re.sub(r"\s+", " ", title.replace("::", ":")).strip() or "Untitled"


def assign_guids(cards: list[Card]) -> None:
    """Stable ids from the question text, so a card keeps its Anki history when the note
    moves folder or its answer is edited. Repeated questions are told apart by path."""
    seen: Counter = Counter()
    for c in cards:
        key = c.kind + "\0" + norm(strip_noise(c.front))
        seen[key] += 1
        n = seen[key]
        material = key if n == 1 else key + "\0" + c.path + "\0" + str(n)
        c.guid = GUID_PREFIX + hashlib.sha1(material.encode()).hexdigest()[:24]


# --------------------------------------------------------------------------- rendering

SR_COMMENT_RE = re.compile(r"<!--SR:.*?-->")
BLOCK_ID_RE = re.compile(r"\s\^[A-Za-z0-9-]+\s*$", re.M)
OBS_COMMENT_RE = re.compile(r"%%.*?%%", re.S)
FLASHCARD_TAG_RE = re.compile(r"(?<!\S)#flashcards(?:/[\w/-]+)?")


def strip_noise(s: str) -> str:
    s = SR_COMMENT_RE.sub("", s)
    s = BLOCK_ID_RE.sub("", s)
    s = OBS_COMMENT_RE.sub("", s)
    return FLASHCARD_TAG_RE.sub("", s)


class Renderer:
    def __init__(self, col=None):
        import markdown

        self.md = markdown.Markdown(extensions=["extra", "sane_lists", "nl2br"])
        self.col = col
        self._media_index: dict[str, Path] | None = None
        self.media_added: set[str] = set()

    def media_index(self) -> dict[str, Path]:
        if self._media_index is None:
            idx: dict[str, Path] = {}
            for dirpath, dirnames, filenames in os.walk(VAULT):
                dirnames[:] = [d for d in dirnames if d not in ALWAYS_SKIP_DIRS]
                for fn in filenames:
                    if Path(fn).suffix.lower() in IMAGE_EXT:
                        idx.setdefault(fn, Path(dirpath) / fn)
            self._media_index = idx
        return self._media_index

    def embed(self, m: re.Match) -> str:
        target, _, opt = m.group(1).partition("|")
        target = target.split("#")[0].strip()
        name = target.rsplit("/", 1)[-1]
        if Path(name).suffix.lower() in IMAGE_EXT:
            src = self.media_index().get(name)
            if src and src.stat().st_size <= MAX_MEDIA_BYTES:
                if self.col is not None:
                    name = self.col.media.add_file(str(src))
                    self.media_added.add(name)
                width = f' width="{opt}"' if opt.isdigit() else ""
                return f'<img src="{html.escape(name)}"{width}>'
        return html.escape(opt or name)

    @staticmethod
    def wikilink(m: re.Match) -> str:
        target, _, alias = m.group(1).partition("|")
        if alias:
            return alias
        page, _, heading = target.partition("#")
        return f"{page} > {heading.lstrip('^')}" if heading and page else (page or heading)

    def to_html(self, s: str, cloze: bool = False) -> str:
        s = strip_noise(s).strip()
        stash: list[str] = []

        def keep(value: str) -> str:
            stash.append(value)
            return f"\x00{len(stash) - 1}\x00"

        # Code spans first, so nothing inside them is rewritten.
        s = re.sub(r"(`+)(.+?)\1", lambda m: keep(f"<code>{html.escape(m.group(2))}</code>"), s)
        s = re.sub(r"\$\$(.+?)\$\$", lambda m: keep("\\[" + html.escape(m.group(1)) + "\\]"), s, flags=re.S)
        s = re.sub(r"(?<![\\$])\$(?=\S)([^$\n]+?)(?<=\S)\$", lambda m: keep("\\(" + html.escape(m.group(1)) + "\\)"), s)
        s = re.sub(r"!\[\[(.+?)\]\]", lambda m: keep(self.embed(m)), s)
        s = re.sub(r"\[\[(.+?)\]\]", lambda m: self.wikilink(m), s)
        s = re.sub(r"\^\[(.+?)\]", lambda m: keep(f" <small>({html.escape(m.group(1))})</small>"), s)
        if cloze:
            s = to_anki_cloze(s, self.cloze_res)
        else:
            s = re.sub(r"==(?=\S)(.+?)(?<=\S)==", r"<mark>\1</mark>", s)
        self.md.reset()
        out = self.md.convert(s)
        return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], out)

    cloze_res: list[re.Pattern] = []


def to_anki_cloze(s: str, cloze_res: list[re.Pattern]) -> str:
    """==a== -> {{c1::a}}; ==2;;a;;hint== -> {{c2::a::hint}}. Unnumbered deletions get
    their own card each, numbered ones are grouped by number, as in clozecraft."""
    used = set()
    for r in cloze_res:
        for m in r.finditer(s):
            if m.groupdict().get("seq"):
                used.add(int(m.group("seq")))
    counter = [0]

    def repl(m: re.Match) -> str:
        seq = m.groupdict().get("seq")
        if seq:
            n = int(seq)
        else:
            counter[0] += 1
            while counter[0] in used:
                counter[0] += 1
            n = counter[0]
        hint = m.groupdict().get("hint")
        return "{{c%d::%s%s}}" % (n, m.group("answer"), "::" + hint if hint else "")

    for r in cloze_res:
        s = r.sub(repl, s)
    return s


def source_html(path: str, context: list[str]) -> str:
    """The breadcrumb shown above each card (note title, then the headings above the card,
    as in the plugin), linking to the note in Obsidian."""
    q = urllib.parse.urlencode({"vault": VAULT_NAME, "file": path[:-3]}, quote_via=urllib.parse.quote)
    crumb = " › ".join(html.escape(c) for c in (context or [Path(path).stem]))
    return f'<a href="obsidian://open?{q}">{crumb}</a>'


def anki_tags(tags: list[str]) -> list[str]:
    out = ["obsidian"]
    for t in tags:
        t = re.sub(r"\s+", "_", t.strip()).replace("/", "::")
        if t:
            out.append(t)
    return sorted(set(out))


# --------------------------------------------------------------------------- anki

CSS = """.card { font-family: system-ui, sans-serif; font-size: 20px; text-align: left;
  color: black; background-color: white; max-width: 46em; margin: auto; }
.nightMode .card, .card.nightMode { color: #ddd; background-color: #222; }
mark { background: #fff3a0; } .nightMode mark { background: #6b5d00; color: #fff; }
.cloze { font-weight: bold; color: #2196f3; }
.osr-context { font-size: 13px; opacity: .65; margin-bottom: 1em; }
.osr-context a { color: inherit; text-decoration: none; }
img { max-width: 100%; }"""
# Bump when the templates or CSS change, so an unchanged vault still triggers a run.
TEMPLATE_VERSION = 2
CONTEXT = '<div class="osr-context">{{Source}}</div>'


def ensure_models(col) -> tuple[dict[str, dict], bool]:
    """Return the three note types, and whether any had to be created. Creating one is a
    schema change, after which Anki only accepts a one-way full sync."""
    mm = col.models
    specs = {
        MODEL_BASIC: (
            0,
            ["Front", "Back", "Source"],
            [("Card 1", CONTEXT + "{{Front}}", "{{FrontSide}}<hr id=answer>{{Back}}")],
        ),
        MODEL_REVERSED: (
            0,
            ["Front", "Back", "Source"],
            [
                ("Card 1", CONTEXT + "{{Front}}", "{{FrontSide}}<hr id=answer>{{Back}}"),
                ("Card 2", CONTEXT + "{{Back}}", "{{FrontSide}}<hr id=answer>{{Front}}"),
            ],
        ),
        MODEL_CLOZE: (1, ["Text", "Source"], [("Cloze", CONTEXT + "{{cloze:Text}}", CONTEXT + "{{cloze:Text}}")]),
    }
    models = {}
    created = False
    for name, (mtype, fields, templates) in specs.items():
        m = mm.by_name(name)
        if m is None:
            created = True
            m = mm.new(name)
            m["type"] = mtype
            for f in fields:
                mm.add_field(m, mm.new_field(f))
            for tname, q, a in templates:
                t = mm.new_template(tname)
                t["qfmt"], t["afmt"] = q, a
                mm.add_template(m, t)
            m["css"] = CSS
            mm.add(m)
            m = mm.by_name(name)
        else:
            # Same fields and templates, only their text differs: a normal change, not a
            # schema change, so it syncs without a full upload.
            changed = m["css"] != CSS
            for t, (_, q, a) in zip(m["tmpls"], templates, strict=False):
                if (t["qfmt"], t["afmt"]) != (q, a):
                    t["qfmt"], t["afmt"] = q, a
                    changed = True
            if changed:
                m["css"] = CSS
                mm.update_dict(m)
                m = mm.by_name(name)
        models[name] = m
    return models, created


def desired_fields(c: Card, r: Renderer) -> tuple[str, dict[str, str]]:
    src = source_html(c.path, c.context)
    if c.kind == "cloze":
        return MODEL_CLOZE, {"Text": r.to_html(c.front, cloze=True), "Source": src}
    model = MODEL_BASIC if c.kind == "basic" else MODEL_REVERSED
    return model, {"Front": r.to_html(c.front), "Back": r.to_html(c.back), "Source": src}


def open_collection():
    from anki.collection import Collection

    HOME_DIR.mkdir(parents=True, exist_ok=True)
    return Collection(str(HOME_DIR / "collection.anki2"))


def load_auth():
    from anki.sync import SyncAuth

    if not AUTH_FILE.exists():
        sys.exit(f"No AnkiWeb key at {AUTH_FILE}. Run: sr_anki_sync.py login")
    d = json.loads(AUTH_FILE.read_text())
    return SyncAuth(hkey=d["hkey"], endpoint=d.get("endpoint") or None)


def save_auth(auth) -> None:
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"hkey": auth.hkey, "endpoint": auth.endpoint}, f)


def sync_once(col, auth, allow_full_upload: bool) -> bool:
    """Sync once. Return True when it was a normal sync (no full sync was needed)."""
    from anki.sync_pb2 import SyncCollectionResponse as R

    from anki.errors import BackendError

    try:
        out = col.sync_collection(auth, sync_media=False)
    except BackendError as e:
        # AnkiWeb refusals (expired key, unconfirmed email, server down) are not bugs here.
        sys.exit(f"AnkiWeb sync failed, nothing was changed on AnkiWeb: {e}")
    if out.new_endpoint:
        auth.endpoint = out.new_endpoint
        save_auth(auth)
    if out.server_message:
        print(f"AnkiWeb says: {out.server_message}")
    req = out.required
    if req in (R.NO_CHANGES, R.NORMAL_SYNC):
        return True
    fresh = col.card_count() == 0
    if req == R.FULL_DOWNLOAD or (req == R.FULL_SYNC and fresh):
        print("Full download from AnkiWeb (the local collection is new or out of date).")
        upload = False
    elif req == R.FULL_UPLOAD and allow_full_upload:
        print("Full upload to AnkiWeb (requested with --allow-full-upload).")
        upload = True
    else:
        name = R.ChangesRequired.Name(req)
        sys.exit(
            f"AnkiWeb asks for {name}. Refusing to choose a side automatically, because a full "
            "upload would overwrite the reviews on AnkiWeb. Resolve it once in Anki desktop, "
            "or delete the local collection to force a fresh download."
        )
    col.close_for_full_sync()
    col.full_upload_or_download(auth=auth, server_usn=None, upload=upload)
    col.reopen(after_full_sync=True)
    return False


# --------------------------------------------------------------------------- commands


def cmd_login(_args) -> None:
    print("AnkiWeb login. The password is used once to get a sync key and is never stored.")
    user = input("AnkiWeb email: ").strip()
    pw = getpass.getpass("AnkiWeb password: ")
    col = open_collection()
    try:
        auth = col.sync_login(user, pw, endpoint=None)
    finally:
        col.close()
    save_auth(auth)
    print(f"Saved the sync key to {AUTH_FILE} (mode 600).")


def cmd_stats(_args) -> None:
    st = SRSettings.load()
    cards = collect(st)
    by_top: Counter = Counter()
    by_kind: Counter = Counter()
    for c in cards:
        parts = c.deck.split("::")
        by_top["::".join(parts[:3])] += 1
        by_kind[c.kind] += 1
    print(f"{len(cards)} notes from {len({c.path for c in cards})} files: {dict(by_kind)}")
    for deck, n in by_top.most_common(40):
        print(f"{n:6d}  {deck}")


def cmd_sync(args) -> None:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(HOME_DIR / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Another sync is running.")

    st = SRSettings.load()
    cards = collect(st)
    if len(cards) < args.min_cards:
        sys.exit(f"Only {len(cards)} cards parsed (< {args.min_cards}). Is the vault complete? Aborting.")

    online = not (args.offline or args.dry_run)
    digest = cards_digest(cards)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if online and not args.force and state.get("pushed_digest") == digest:
        if not STATS_DIR:
            print(f"{len(cards)} vault cards, unchanged since the last push. Nothing to do.")
            return
        # Nothing to push, but the review statistics need the reviews from AnkiWeb.
        auth = load_auth()
        col = open_collection()
        try:
            sync_once(col, auth, False)
            print(f"{len(cards)} vault cards, unchanged since the last push. Pulled reviews only.")
            export_stats(col)
        finally:
            col.close()
        return
    auth = load_auth() if online else None
    col = open_collection()
    try:
        pulled_clean = sync_once(col, auth, args.allow_full_upload) if online else False
        models, created_models = ensure_models(col) if not args.dry_run else ({}, False)
        r = Renderer(col if not args.dry_run else None)
        r.cloze_res = [cloze_regex(p) for p in st.cloze_patterns]

        existing: dict[str, int] = {}
        for name in (MODEL_BASIC, MODEL_REVERSED, MODEL_CLOZE):
            for nid in col.find_notes(f'"note:{name}"'):
                existing[col.get_note(nid).guid] = nid

        wanted = {c.guid for c in cards}
        to_delete = [nid for g, nid in existing.items() if g not in wanted]
        if existing and len(to_delete) > max(args.max_deletes, len(existing) // 10) and not args.allow_deletes:
            sys.exit(
                f"{len(to_delete)} of {len(existing)} Obsidian notes would be deleted. That looks like "
                "a broken vault copy, so nothing was changed. Re-run with --allow-deletes if intended."
            )

        added = updated = moved = 0
        deck_ids: dict[str, int] = {}
        for c in cards:
            model_name, fields = desired_fields(c, r)
            tags = anki_tags(c.tags)
            nid = existing.get(c.guid)
            if args.dry_run:
                if nid is None:
                    added += 1
                continue
            if c.deck not in deck_ids:
                deck_ids[c.deck] = col.decks.id(c.deck)
            did = deck_ids[c.deck]
            if nid is None:
                note = col.new_note(models[model_name])
                note.guid = c.guid
                for k, v in fields.items():
                    note[k] = v
                note.tags = tags
                col.add_note(note, did)
                added += 1
                continue
            note = col.get_note(nid)
            if note.note_type()["name"] != model_name:
                # The card type changed (for example :: became :::). Replace the note.
                col.remove_notes([nid])
                note = col.new_note(models[model_name])
                note.guid = c.guid
                for k, v in fields.items():
                    note[k] = v
                note.tags = tags
                col.add_note(note, did)
                updated += 1
                continue
            if any(note[k] != v for k, v in fields.items()) or sorted(note.tags) != tags:
                for k, v in fields.items():
                    note[k] = v
                note.tags = tags
                col.update_note(note)
                updated += 1
            cids = [cid for cid in note.card_ids() if col.get_card(cid).did != did]
            if cids:
                col.set_deck(cids, did)
                moved += 1

        if not args.dry_run and to_delete:
            col.remove_notes(to_delete)
        if not args.dry_run:
            remove_empty_decks(col)
            apply_deck_options(col)
            scheme = NEW_ORDER if NEW_ORDER in ("newest", "oldest") else ""
            if scheme and state.get("positions_by_date") != 1:
                print(f"Re-numbered {reposition_new_cards(col, cards)} unseen cards by note date.")
                state["positions_by_date"] = 1
        verb = "would be" if args.dry_run else "were"
        print(
            f"{len(cards)} vault cards. {added} notes {verb} added, {updated} updated, "
            f"{moved} moved, {len(to_delete)} deleted."
        )
        if online:
            # Creating the note types (first run only) makes Anki demand a full upload.
            # That is safe when this same run pulled cleanly a moment ago: the local
            # collection then holds everything AnkiWeb had plus the vault cards.
            upload_ok = args.allow_full_upload or (created_models and pulled_clean)
            sync_once(col, auth, upload_ok)
            col.sync_media(auth)
            state["pushed_digest"] = digest
            STATE_FILE.write_text(json.dumps(state))
            print("Synced with AnkiWeb.")
        if not args.dry_run:
            export_stats(col)
    finally:
        col.close()


REVLOG_KINDS = {0: "learn", 1: "review", 2: "relearn", 3: "filtered", 4: "manual", 5: "rescheduled"}
STATS_FIELDS = ["time", "card_id", "note_path", "note_title", "deck", "kind", "first_review", "button", "seconds", "interval_days"]


def source_path(source: str) -> str:
    """The vault path stored in a card's Source link, with the .md extension."""
    m = re.search(r"[?&]file=([^\"&]+)", source)
    return urllib.parse.unquote(m.group(1)) + ".md" if m else ""


def review_rows(col, since_ms: int) -> dict[str, list[dict]]:
    """Reviews since the given time, grouped by local calendar day."""
    first = dict(col.db.all("select cid, min(id) from revlog group by cid"))
    days: dict[str, list[dict]] = {}
    sql = "select id, cid, ease, ivl, time, type from revlog where id >= ? order by id"
    for rid, cid, ease, ivl, ms, rtype in col.db.all(sql, since_ms):
        stamp = dt.datetime.fromtimestamp(rid / 1000)
        path = title = deck = ""
        try:
            card = col.get_card(cid)
            note = card.note()
            deck = col.decks.name(card.did)
            if "Source" in note.keys():
                path = source_path(note["Source"])
                title = Path(path).stem
        except Exception:
            pass  # the card was deleted since, the review still counts
        days.setdefault(stamp.strftime("%Y-%m-%d"), []).append(
            {
                "time": stamp.strftime("%Y-%m-%dT%H:%M:%S"),
                "card_id": cid,
                "note_path": path,
                "note_title": title,
                "deck": deck,
                "kind": REVLOG_KINDS.get(rtype, str(rtype)),
                "first_review": int(first.get(cid) == rid),
                "button": ease,
                "seconds": round(ms / 1000, 1),
                # Anki stores learning intervals as negative seconds.
                "interval_days": ivl if ivl >= 0 else round(-ivl / 86400, 3),
            }
        )
    return days


def export_stats(col) -> None:
    if not STATS_DIR:
        return
    base = Path(STATS_DIR) if Path(STATS_DIR).is_absolute() else VAULT / STATS_DIR
    start = dt.datetime.combine(dt.date.today() - dt.timedelta(days=STATS_DAYS - 1), dt.time())
    written = 0
    for day, rows in review_rows(col, int(start.timestamp() * 1000)).items():
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=STATS_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        target = base / day / f"Anki_Reviews_{day}.csv"
        if target.exists() and target.read_text() == buf.getvalue():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(buf.getvalue())
        written += 1
    print(f"Review statistics: {written} day file(s) updated in {base}.")


def cards_digest(cards: list[Card]) -> str:
    h = hashlib.sha1()
    # The managed options are part of the digest, so changing one triggers a run.
    h.update(json.dumps([NEW_PER_DAY, REVIEWS_PER_DAY, NEW_ORDER, ROOT_DECK, TEMPLATE_VERSION, CONTEXT_SKIP.pattern, NOTE_DECKS]).encode())
    for c in cards:
        h.update("\0".join([c.guid, c.kind, c.front, c.back, c.path, c.deck, ",".join(c.tags), *c.context]).encode())
    h.update(VAULT_NAME.encode())
    return h.hexdigest()


def apply_deck_options(col) -> None:
    """Give every deck under ROOT_DECK one shared preset with the configured options."""
    if NEW_PER_DAY is None and REVIEWS_PER_DAY is None and not NEW_ORDER:
        return
    conf = next((c for c in col.decks.all_config() if c["name"] == ROOT_DECK), None)
    if conf is None:
        conf = col.decks.get_config(col.decks.add_config_returning_id(ROOT_DECK))
    if NEW_PER_DAY is not None:
        conf["new"]["perDay"] = NEW_PER_DAY
    if REVIEWS_PER_DAY is not None:
        conf["rev"]["perDay"] = REVIEWS_PER_DAY
    if NEW_ORDER in ("newest", "oldest"):
        # Gather by highest (newest) or lowest position, then keep the gathered order.
        conf["newGatherPriority"] = 2 if NEW_ORDER == "newest" else 1
        conf["newSortOrder"] = 1
    col.decks.update_config(conf)
    for d in col.decks.all_names_and_ids():
        if d.name == ROOT_DECK or d.name.startswith(ROOT_DECK + "::"):
            deck = col.decks.get(d.id)
            if deck.get("conf") != conf["id"]:
                col.decks.set_config_id_for_deck_dict(deck, conf["id"])


def reposition_new_cards(col, cards: list[Card]) -> int:
    """Number the unseen cards by note creation date (oldest lowest), so the gather order
    above can show the newest or the oldest notes first. Cards added by later runs get
    the next positions anyway, so this only has to run when the scheme changes."""
    created = {c.guid: (c.created, c.path, c.guid) for c in cards}
    new_cids = col.find_cards(f'"deck:{ROOT_DECK}" is:new')
    if not new_cids:
        return 0
    keyed = []
    for cid in new_cids:
        card = col.get_card(cid)
        key = created.get(card.note().guid)
        if key is not None:
            keyed.append((key, card.ord, cid))
    keyed.sort()
    col.sched.reposition_new_cards(
        [cid for _, _, cid in keyed], starting_from=0, step_size=1, randomize=False, shift_existing=False
    )
    return len(keyed)


def remove_empty_decks(col) -> None:
    # By id, not by a "deck:" search: note titles can contain " * _ which a search misreads.
    for d in sorted(col.decks.all_names_and_ids(), key=lambda d: -d.name.count("::")):
        if not d.name.startswith(ROOT_DECK + "::"):
            continue
        if not col.decks.card_count(d.id, include_subdecks=True):
            col.decks.remove([d.id])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="store an AnkiWeb sync key").set_defaults(func=cmd_login)
    sub.add_parser("stats", help="count the vault's cards").set_defaults(func=cmd_stats)
    p = sub.add_parser("sync", help="pull, apply the vault, push")
    p.add_argument("--dry-run", action="store_true", help="no writes, no network")
    p.add_argument("--offline", action="store_true", help="update the local collection only")
    p.add_argument("--allow-deletes", action="store_true", help="skip the mass-deletion guard")
    p.add_argument("--max-deletes", type=int, default=200, help="deletions allowed without the flag")
    p.add_argument("--min-cards", type=int, default=100, help="abort below this many parsed cards")
    p.add_argument("--force", action="store_true", help="sync even if the vault cards are unchanged")
    p.add_argument("--allow-full-upload", action="store_true", help="permit a one-way full upload")
    p.set_defaults(func=cmd_sync)
    for sp in sub.choices.values():
        sp.add_argument("--vault", help="the Obsidian vault (default: $SR_ANKI_VAULT)")
    args = ap.parse_args()
    global VAULT, SR_DATA, VAULT_NAME
    if args.vault:
        VAULT = Path(args.vault).expanduser()
        SR_DATA = VAULT / ".obsidian/plugins/obsidian-spaced-repetition/data.json"
        VAULT_NAME = os.environ.get("SR_ANKI_VAULT_NAME", VAULT.name)
    if args.func is not cmd_login and not SR_DATA.exists():
        sys.exit(f"No spaced-repetition plugin settings at {SR_DATA}. Set SR_ANKI_VAULT or --vault.")
    args.func(args)


if __name__ == "__main__":
    main()
