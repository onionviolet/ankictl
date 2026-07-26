# ankictl

**Let an AI tutor read and write your [Anki](https://apps.ankiweb.net/) collection.** A single file, no dependencies, over the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) addon.

Most AI tutoring is amnesiac. The model explains something well, the session ends, and nothing carries. It cannot tell what stuck, so next time it either re-teaches what you already know or skips what you quietly forgot.

Anki has the missing half. Its review log is a per-fact record of what *you specifically* do not know, gathered over months and graded by your actual recall rather than your opinion of your recall. That distinction is the whole reason spaced repetition works.

So: Anki owns **what is known**. The model owns **why it is not sticking, and what to do about it**. This is the bridge.

```
$ ankictl.py weak --min-lapses 3
7 struggling card(s), worst first:

  lapses=6   ease=180%  ivl=2d  [Chemistry::Organic]
    Front: What does SN1 stereochemistry produce?
  lapses=5   ease=185%  ivl=3d  [Chemistry::Organic]
    Front: What does SN2 stereochemistry produce?

Look for what these have in common before explaining them one by one.
Repeated lapses usually mean two facts are competing, not that one is hard.
```

A model reads that, notices the two are not individually hard but are being confused *with each other*, and writes one card that draws the boundary:

```
$ ankictl.py add --file card.json --apply
added 1 note(s).
```

That loop, and the reasoning it depends on, is documented in **[AGENTS.md](AGENTS.md)**.

It also does plain collection maintenance, including `audit`, which finds cards sitting in the wrong deck with no configuration at all.

## Install

Python 3.9+, no packages. Anki must be running with AnkiConnect installed (Anki, then Tools, Add-ons, Get Add-ons, code `2055492159`, restart).

```
curl -O https://raw.githubusercontent.com/onionviolet/ankictl/main/ankictl.py
python ankictl.py ping
```

## Commands

**Reading**

| Command | Does |
|---|---|
| `ping` | confirm Anki and AnkiConnect are reachable |
| `stats` | decks, note types with their fields, counts. Call this first |
| `weak [query]` | what the learner keeps failing, worst first |
| `history [query]` | the review log itself: at what interval and what hour it fails |
| `decks` | deck tree with new / learning / due counts |
| `audit [query]` | find cards sitting in the wrong deck |
| `find "<search>"` | list notes matching an Anki search |
| `fields --notetype N` | list a note type's fields |
| `template --notetype N` | card templates and styling |
| `tags [query]` | tags in use, with counts |
| `retention --deck D` | scheduler tuning on that deck's preset |
| `audio --deck D` | autoplay / replay behaviour on that deck's preset |

**Writing** (dry run until `--apply`)

| Command | Does |
|---|---|
| `add --file notes.json` | create notes; `-` reads stdin |
| `update --file edits.json` | rewrite fields, keeping review history |
| `move "<search>" --to "<deck>"` | change deck, preserving scheduling |
| `suspend` / `unsuspend "<search>"` | take cards out of / back into rotation |
| `limits --deck D [--new N --rev N]` | show or set daily limits |
| `preset --deck D [D2 ...] --clone NAME` | give decks their own options preset |
| `addfield --notetype N --field F` | append a field |
| `template --notetype N --set-front F` | rewrite a card template or its CSS |
| `tags "<search>" --add T` | add / remove tags; `--rename` is collection-wide |
| `reschedule "<search>" --days N` | move due dates; `--forget` resets to new |
| `retention --deck D --target 0.9` | desired retention, max interval, leech threshold |
| `audio --deck D --autoplay off` | turn every `{{tts}}` and `[sound:]` into a play button |

Every command takes `--json` for machine-readable output, which is what an agent should use. Searches use the same syntax as Anki's Browse bar: `deck:Spanish`, `tag:chem::*`, `note:Cloze`, `-deck:Spanish::*`.

**There is no delete command, deliberately.** Suspension is reversible; deletion throws away review history that took months to accumulate. An agent should not be able to do that in one call. Deleting stays a human action in the GUI, where it can be undone.

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

## `weak` and `history` read different things

`weak` reads the counters Anki keeps on each card: lapses, ease factor, current interval. Those are a compression, and they flatten failure modes that want opposite fixes.

`history` reads the review log, which keeps every individual answer: which button, at what interval the card had reached, at what hour, how many seconds it took. Three cards can all show `lapses=4` and be three different problems.

- Failing **only past long intervals** is a consolidation ceiling. The card is fine; the interval outran it. Lower the maximum interval or raise desired retention for that deck.
- Failing **at every interval, including short ones** was never encoded. That is a card problem, and usually an interference problem with a neighbouring card. Rewrite it as a discrimination.
- Failing **only in one hour band** is not a card problem at all.

The last one is why the hour breakdown is there and why it is worth being suspicious of it: a difference across hours is a claim about the reviewer, not the material, and it needs a real sample before it means anything. `history` prints a warning below 200 graded reviews for that reason. Rates over a few dozen reviews are noise, and a tutor acting on noise will confidently rewrite cards that were never the problem.

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

If your port falls inside a listed range, pick one that does not (Anki, Tools, Add-ons, AnkiConnect, Config, `webBindPort`) and restart Anki. You do not then have to tell this tool where you moved it: when the default port refuses a connection, it reads `webBindPort` back out of the addon's own `meta.json` and retries there. Set `ANKI_CONNECT_URL` only for a non-local host or a non-standard setup; doing so pins the URL and disables the fallback.

```
set ANKI_CONNECT_URL=http://127.0.0.1:8900
```

The reservations are reshuffled on reboot, so a port that works today can break later. The durable fix is to push Windows' dynamic port range up out of the way, which stops Hyper-V allocating low blocks at all. Run as admin, then reboot:

```
netsh int ipv4 set dynamic tcp start=49152 num=16384
```

## On model choice

This tool hands a model destructive verbs and a search language that fails quietly. That makes capability load-bearing rather than a nice-to-have, for three specific reasons:

- **Anki search has traps that return zero results instead of an error.** A tag written `topic/sub` needs `tag:topic/*`; the `::` hierarchy wildcard matches nothing against it. `deck:X` includes subdecks unless excluded. A model that writes a plausible query and does not check the count will move the wrong cards, or none, and report success either way.
- **Diagnosis is not retrieval.** Noticing that six failing cards share one confusable boundary, rather than being six independently hard facts, means holding them together and reasoning about interference. Weaker models reliably default to re-explaining each card in turn, which looks like tutoring and does not teach.
- **Fabrication is unusually expensive here.** A wrong fact in a chat reply is read once. A wrong fact written into a card is rehearsed on a spaced schedule, on purpose, for months. Spaced repetition installs errors exactly as well as it installs facts.

Use a frontier model, pass `--json`, and read the dry run.

## Why it exists

A batch of Cloze cards imported into the wrong deck and sat there for weeks, quietly inflating an unrelated review queue. The cause was a missing `#deck:` line in an import header: Anki falls back to whatever deck happens to be selected. A missing `#notetype:` throws a visible error, a missing `#deck:` fails silently.

Finding that by clicking through Browse is tedious and depends on noticing. `audit` makes it mechanical. The rest of the tool followed from a better question: if a model can already see the collection, why is it not teaching from it?

Designed and written by [Claude Opus 5](https://www.anthropic.com/claude) with its author, against that real collection. Two of the design decisions here came from things going wrong mid-build rather than from planning, and both are documented in [AGENTS.md](AGENTS.md#provenance).

## License

MIT.
