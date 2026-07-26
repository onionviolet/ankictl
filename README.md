# ankictl

A single-file, dependency-free CLI for reading and repairing a live [Anki](https://apps.ankiweb.net/) collection from the shell, over the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) addon.

Written after 38 Security+ cards were discovered sitting in a Mandarin deck, where they had been quietly inflating the wrong review queue for weeks. Finding that by clicking through the Browse window is tedious; finding it with `ankictl audit` takes a second, and the same command now catches the whole class of mistake.

```
$ ankictl.py audit
1 misfiled note(s):

  in 'Mandarin' -> belongs under 'Sec+'  [Cloze] tags=sec-plus/iam prio-h
    {{c1::Something you know}} (password), {{c2::something you have}} (token)...

fix e.g.:  ankictl.py move "deck:Mandarin tag:sec-plus::*" --to "Sec+::SY0-701" --apply
```

## Install

Python 3.9+, no packages. Anki must be running with AnkiConnect installed (Anki → Tools → Add-ons → Get Add-ons → `2055492159` → restart).

```
curl -O https://raw.githubusercontent.com/onionviolet/ankictl/main/ankictl.py
python ankictl.py ping
```

## Commands

| Command | Does |
|---|---|
| `ping` | confirm Anki and AnkiConnect are reachable |
| `decks` | deck tree with new / learning / due counts |
| `audit` | find cards sitting in the wrong deck |
| `find "<search>"` | list notes matching an Anki search |
| `move "<search>" --to "<deck>"` | change deck, preserving scheduling |
| `limits --deck D [--new N --rev N]` | show or set daily limits |
| `preset --deck D --clone "<name>"` | give a deck its own options preset |
| `fields --notetype N` | list a note type's fields |
| `addfield --notetype N --field F` | append a field |

Searches use the same syntax as Anki's Browse bar: `deck:Mandarin`, `tag:sec-plus::*`, `note:Cloze`, `-deck:Mandarin::*`.

**Every mutating command is a dry run until you pass `--apply`.**

## Three things it encodes

**It never writes to `collection.anki2`.** A running Anki holds that file open with a write-ahead log and keeps collection state in memory. A direct SQLite write risks corruption, and whatever survives gets overwritten on the next flush. All writes go through the running process. (If you do want to inspect a closed collection with `sqlite3`, work on a copy, and register a `unicase` collation first or every `ORDER BY` on a text column raises.)

**`limits` refuses to write to the shared `Default` preset.** Deck options in Anki are *presets*, not per-deck settings. Raising one deck's daily limits silently raises every deck sharing that preset. Clone first with `preset`, or pass `--force` if you mean it.

**`audit` is configurable.** The `OWNERSHIP` table at the top of the file maps a note type or tag prefix to the deck prefix it belongs under. Edit it to match your collection.

## Notes on daily limits

Two counts in Anki's deck list are limits; one is not.

- **Blue (new)** — `New cards/day`.
- **Green (review)** — `Maximum reviews/day`. Under the v3 scheduler this *also* gates new cards, because a new card becomes a review card. Raising new/day while the review cap sits at the default 200 does nothing. `limits --new N --rev N` sets both.
- **Red (learning)** — no limit exists. Learning and relearning cards always appear when due; the count is a product of your learning steps.

`New Cards Ignore Review Limit` is a collection-level setting and stays a GUI checkbox under Deck Options → Daily Limits.

## Troubleshooting: "Failed to listen on port 8765"

On Windows this usually does **not** mean something is using the port. Hyper-V, WSL, and Docker reserve large TCP blocks at boot, and 8765 sits at the top of a commonly-reserved range (8666–8765). A bind into a reserved range fails with `WSAEACCES` (10013), which AnkiConnect reports as "in use." `netstat` shows nothing listening, because nothing is.

Confirm it:

```
netsh interface ipv4 show excludedportrange protocol=tcp
```

If your port falls inside a listed range, pick one that doesn't (Anki → Tools → Add-ons → AnkiConnect → Config → `webBindPort`), restart Anki, and point the tool at it:

```
set ANKI_CONNECT_URL=http://127.0.0.1:8900
```

The reservations are reshuffled on reboot, so a port that works today can break later. The durable fix is to push Windows' dynamic port range up out of the way (`netsh int ipv4 set dynamic tcp start=49152 num=16384`, admin, then reboot), which stops Hyper-V from allocating low blocks at all.

## License

MIT.
