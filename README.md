# ankictl

A single-file, dependency-free CLI for reading and repairing a live [Anki](https://apps.ankiweb.net/) collection from the shell, over the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) addon.

The headline command is `audit`, which finds cards sitting in the wrong deck. It needs no configuration and knows nothing about your decks in advance.

```
$ ankictl.py audit
11 misfiled note(s) out of 647 checked:

  in 'Spanish' -> expected under 'Chemistry'   (tag 'chem' is 97% in 'Chemistry')
    [Cloze] {{c1::Le Chatelier's principle}} states that a system at equilibrium...

suggested fixes (dry run without --apply):
  ankictl.py move "deck:Spanish -deck:Spanish::* tag:chem*" --to Chemistry    # 11 note(s)
```

## Install

Python 3.9+, no packages. Anki must be running with AnkiConnect installed (Anki, then Tools, Add-ons, Get Add-ons, code `2055492159`, restart).

```
curl -O https://raw.githubusercontent.com/onionviolet/ankictl/main/ankictl.py
python ankictl.py ping
```

## Commands

| Command | Does |
|---|---|
| `ping` | confirm Anki and AnkiConnect are reachable |
| `decks` | deck tree with new / learning / due counts |
| `audit [query]` | find cards sitting in the wrong deck |
| `find "<search>"` | list notes matching an Anki search |
| `move "<search>" --to "<deck>"` | change deck, preserving scheduling |
| `limits --deck D [--new N --rev N]` | show or set daily limits |
| `preset --deck D [D2 ...] --clone "<name>"` | give decks their own options preset |
| `fields --notetype N` | list a note type's fields |
| `addfield --notetype N --field F` | append a field |

Searches use the same syntax as Anki's Browse bar: `deck:Spanish`, `tag:chem::*`, `note:Cloze`, `-deck:Spanish::*`.

**Every mutating command is a dry run until you pass `--apply`.**

## How `audit` works without configuration

A note type or a tag is a claim about what a note **is**. Its deck is a claim about where it **lives**. In a healthy collection those two agree almost perfectly, so the tool learns each label's home deck from the collection itself, then reports the stragglers. Nothing is hardcoded, and it adapts as you rename or add decks.

The part that makes it usable is **consensus**. Some labels are orthogonal to subject: a priority tag, `trap`, `leech`, or a note type shared across every deck. Those spread everywhere, so a naive reading makes them look like they "belong" to whichever deck is largest, and every note elsewhere gets indicted. A worked example from development: a `prio-m` priority tag read as 99% owned by the biggest deck purely because that deck held 65% of the collection, and the first version flagged four correctly-filed notes because of it.

So a note is reported only when **none** of its labels vouches for the deck it is actually in. One label dissenting while another agrees means you have a cross-cutting tag, not a misfile. That single rule took the false positives to zero on the collection it was built against, while still catching every genuine misfile.

Two knobs if the defaults do not suit your collection:

- `--min-share` (default 0.9): how dominant a label's home deck must be before outliers are judged. Lower finds more, and more noise.
- `--min-notes` (default 5): ignore labels with too few notes to say anything.

Pass a query to scope it: `ankictl.py audit "deck:Chemistry"`.

## Three things it encodes

**It never writes to `collection.anki2`.** A running Anki holds that file open with a write-ahead log and keeps collection state in memory. A direct SQLite write risks corruption, and whatever survives gets overwritten on the next flush. All writes go through the running process. (If you do want to inspect a closed collection with `sqlite3`, work on a copy, and register a `unicase` collation first or every `ORDER BY` on a text column raises.)

**`limits` refuses to write to the shared `Default` preset.** Deck options in Anki are *presets*, not per-deck settings. Raising one deck's daily limits silently raises every deck sharing that preset. Clone first with `preset`, or pass `--force` if you mean it.

**`preset` takes several decks at once,** because under the v3 scheduler a deck is bound by its own limits *and* by every parent's. Giving a subdeck a roomy preset while its parent keeps the default leaves it throttled by the parent. Pass the whole chain.

## Notes on daily limits

Two counts in Anki's deck list are limits. One is not.

- **Blue (new):** `New cards/day`.
- **Green (review):** `Maximum reviews/day`. Under the v3 scheduler this *also* gates new cards, because a new card becomes a review card. Raising new/day while the review cap sits at the default 200 does nothing at all. `limits --new N --rev N` sets both together for exactly this reason.
- **Red (learning):** no limit exists. Learning and relearning cards always appear when due; the count is a product of your learning steps, not a cap.

`New Cards Ignore Review Limit` is a collection-level setting and stays a GUI checkbox under Deck Options, Daily Limits.

## Troubleshooting: "Failed to listen on port 8765"

On Windows this usually does **not** mean something is using the port. Hyper-V, WSL, and Docker reserve large TCP blocks at boot, and 8765 sits at the top of a commonly reserved range (8666 to 8765). A bind into a reserved range fails with `WSAEACCES` (10013), *permission denied*, which AnkiConnect reports as "in use". `netstat` shows nothing listening, because nothing is.

Confirm it:

```
netsh interface ipv4 show excludedportrange protocol=tcp
```

If your port falls inside a listed range, pick one that does not (Anki, Tools, Add-ons, AnkiConnect, Config, `webBindPort`), restart Anki, and point the tool at it:

```
set ANKI_CONNECT_URL=http://127.0.0.1:8900
```

The reservations are reshuffled on reboot, so a port that works today can break later. The durable fix is to push Windows' dynamic port range up out of the way, which stops Hyper-V allocating low blocks at all. Run as admin, then reboot:

```
netsh int ipv4 set dynamic tcp start=49152 num=16384
```

## Why it exists

A batch of Cloze cards imported into the wrong deck and sat there for weeks, quietly inflating an unrelated review queue. The cause was a missing `#deck:` line in the import file's header block: Anki falls back to whatever deck happens to be selected. A missing `#notetype:` throws a visible error, a missing `#deck:` fails silently.

Finding that by clicking through the Browse window is tedious and depends on noticing. `audit` makes it mechanical.

## License

MIT.
