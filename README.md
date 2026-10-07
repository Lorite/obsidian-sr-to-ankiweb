# obsidian-sr-to-ankiweb

Review the flashcards of the Obsidian [Spaced Repetition plugin](https://github.com/st3v3nmw/obsidian-spaced-repetition) in **AnkiDroid, AnkiWeb or Anki desktop**, on any device, without opening Obsidian.

`sr-anki-sync` reads your vault, finds the cards exactly as the plugin does, writes them into a local Anki collection and syncs that collection with AnkiWeb. It runs headless (a cron job or a systemd timer on any always-on machine is enough). It needs neither Obsidian, nor Anki desktop, nor AnkiConnect, because it uses the official [`anki`](https://pypi.org/project/anki/) Python package, which includes AnkiWeb sync.

Context: [obsidian-spaced-repetition#27](https://github.com/st3v3nmw/obsidian-spaced-repetition/issues/27) asks for an Anki export. This tool is an external, one-way alternative to that.

## How it works

- **One-way for content, Anki owns the scheduling.** The vault is the source of truth for the card text. Every run first pulls from AnkiWeb (so reviews made on your phone are kept), then applies the vault, then pushes. Reviews never flow back into the plugin.
- **Same cards as the plugin.** The plugin's parser (`src/parser.ts`) is ported line for line. Separators, cloze patterns and ignored folders come from your vault's `.obsidian/plugins/obsidian-spaced-repetition/data.json`.
- **Supported card types:** `::`, `:::` (both directions), multi-line `?` and `??`, and clozes (highlights, bold or curly brackets, as enabled in the plugin, including `==2;;answer;;hint==`).
- **Decks, as in the plugin.** With *Convert folders to decks* on, the folder is the deck: `work/concepts/x.md` lands in `Obsidian::work::concepts`. With it off (the plugin's default), only notes tagged with a flashcard tag are read, and the tag is the deck: `#flashcards/spanish` lands in `Obsidian::flashcards::spanish`. Note tags also become Anki tags (`a/b` becomes `a::b`), so you can build filtered decks by tag.
- **Where a card comes from.** Like the plugin's *Show context in cards*, each card starts with a small breadcrumb: the note title, then the headings above the card (`Note › Section › Subsection`). Tap it to open the note in Obsidian (`obsidian://open`). With the plugin setting off, only the note title is shown.
- **Rendering.** Markdown becomes HTML, `$…$` and `$$…$$` become MathJax, `![[image.png]]` embeds become Anki media, and `[[links]]` become plain text.
- **Two filters the plugin does not have.** Dataview inline fields (`(habit:: true)`, `status:: done`) are not cards, and a cloze needs a non-space character just inside its markers, so `a == b and c == d` is not a cloze.

## Safety

- It only creates, changes and deletes notes of its own three note types (`Obsidian SR Basic`, `Obsidian SR Reversed`, `Obsidian SR Cloze`). Your other decks and notes are never touched.
- A card's identity is a hash of its type and question text. Moving a note or editing an answer keeps the review history. Editing the question creates a new card. A question repeated in several notes is told apart by path, and the first occurrence keeps the plain id.
- When AnkiWeb asks for a full sync, it downloads into a new empty collection. It uploads only when the same run pulled cleanly a moment earlier and the upload is required because it created its note types (the first run), or when you pass `--allow-full-upload`. In any other case it stops and tells you why.
- It aborts when more than 200 notes (or 10 %) would be deleted, unless `--allow-deletes`, and when fewer than 100 cards parse (`--min-cards`), which protects against a half-synced vault copy.
- A run whose cards and options are unchanged since the last push does not contact AnkiWeb at all.
- The AnkiWeb password is used once to get a sync key and is never stored.

## Install and first run

```bash
uv tool install git+https://github.com/Lorite/obsidian-sr-to-ankiweb
export SR_ANKI_VAULT=~/path/to/vault
sr-anki-sync login          # asks for the AnkiWeb email and password
sr-anki-sync stats          # card counts per deck, no Anki access
sr-anki-sync sync --dry-run # what would change, no writes, no network
sr-anki-sync sync
```

`pip install git+https://github.com/Lorite/obsidian-sr-to-ankiweb` works too. Python 3.10 or newer.

Use one machine only for the sync. Two machines pushing to the same AnkiWeb account will collide. A copy of the vault from Syncthing, Obsidian Sync or git is fine, the tool only reads it. Then keep it running with a timer, see [`examples/`](examples/).

## Settings

All settings are environment variables. `--vault` overrides `SR_ANKI_VAULT`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SR_ANKI_VAULT` | (required) | Path to the Obsidian vault |
| `SR_ANKI_HOME` | `~/.local/share/sr-anki-sync` | Local Anki collection and state |
| `SR_ANKI_AUTH` | `~/.config/sr-anki-sync/ankiweb-auth.json` | AnkiWeb sync key (mode 600) |
| `SR_ANKI_ROOT_DECK` | `Obsidian` | Parent deck of all generated decks |
| `SR_ANKI_EXCLUDE` | (none) | Comma-separated path prefixes to leave out, e.g. `Daily/,Templates/` |
| `SR_ANKI_VAULT_NAME` | vault folder name | Vault name in the `obsidian://` links |
| `SR_ANKI_NOTE_DECKS` | off | `1` puts each note's cards in their own deck under the folder deck, e.g. `Obsidian::work::concepts::Entropy`. Studying the folder deck still includes them all |
| `SR_ANKI_STATS_DIR` | (off) | Write one CSV of reviews per calendar day to `<dir>/<YYYY-MM-DD>/Anki_Reviews_<YYYY-MM-DD>.csv` (relative to the vault), for daily notes. Every run then pulls from AnkiWeb |
| `SR_ANKI_STATS_DAYS` | `7` | How many recent days the statistics export rewrites |
| `SR_ANKI_NEW_PER_DAY` | (unchanged) | New cards per day for every generated deck |
| `SR_ANKI_REVIEWS_PER_DAY` | (unchanged) | Maximum reviews per day for every generated deck |
| `SR_ANKI_CONTEXT_SKIP` | (none) | Regex of headings to hide in the breadcrumb, e.g. `Flashcards\|AI Generated.*` |
| `SR_ANKI_NEW_ORDER` | (unchanged) | `newest` shows cards from the most recently created notes first, `oldest` the opposite |

The three deck options are kept in one Anki preset named after the root deck. If none of them is set, the tool leaves the deck options alone, so you can manage them in Anki instead. The note's creation date comes from the frontmatter (`created`, `date_created` or `date`), or else from the file's modification time.

## Limitations

- One-way: a card edited in Anki is overwritten by the vault version on the next change, and reviews never reach the plugin.
- No image occlusion and no audio.
- Per-card deck tags (a `#flashcards/deck` tag at the start of a single card) are not supported yet. Such a card goes to its note's deck.

## Tests

```bash
uv run --with anki --with markdown --with pyyaml --with pytest python -m pytest -q tests
```

## License

MIT. The parser is a port of obsidian-spaced-repetition by Stephen Mwangi (MIT), see [LICENSE](LICENSE).
