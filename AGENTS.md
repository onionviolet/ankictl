# Using ankictl as an AI tutor

This file is the usage contract for a language model driving `ankictl`. It is written to be read by an agent, and it is also the honest description of what this tool is for.

## Why Anki is the right substrate

Most AI tutoring is amnesiac. The model explains something well, the session ends, and nothing carries. It cannot tell what stuck, so the next session either re-teaches what is already known or skips what was quietly forgotten.

Anki has already solved the missing half. Its review log is a per-fact record of what this specific person does not know, gathered over months, graded by their own recall rather than their self-assessment. That last part matters: people are bad at knowing what they know, which is the whole reason spaced repetition exists.

So the division of labour is clean. Anki owns *what is known*. The model owns *why it is not sticking, and what to do about it*. `ankictl` is the bridge.

## The loop

```
ankictl.py stats --json                     # orient: real deck and notetype names
ankictl.py weak --json --min-lapses 3       # what is actually failing
   ... reason about what the failures have in common ...
ankictl.py add --file new-cards.json        # dry run first
ankictl.py add --file new-cards.json --apply
```

### 1. Orient before writing

Call `stats --json` first. It returns exact deck names, note types, and their field names. Writing a card to a guessed deck name fails; writing to a real one that is wrong for the content is worse, because it succeeds silently. Never invent a deck or field name, and never create a deck as a side effect of adding cards.

### 2. Read the signal

`weak --json` ranks reviewed cards by lapses, then by ease factor.

- **lapses** = how many times a mature card was forgotten. The strongest signal in the collection.
- **ease** = the scheduler's own verdict, in percent. Below about 200% means it has repeatedly been told this is hard.
- Cards tagged `leech` have failed so often Anki gave up on them.

Scope it with a query when the learner is working on one subject: `ankictl.py weak "deck:Chemistry" --json`.

### 2b. Then read the log, not just the counters

`weak` gives you lapse counts. `history --json` gives you the reviews those counts were computed from, and the difference decides what you should do about them.

```
ankictl.py history "deck:Sec+" --json
```

Three fields carry most of the signal:

- **`byInterval`** is the important one. A card failing only in the `22-60d` and `60d+` buckets has a consolidation ceiling, and the fix is scheduling (`retention --target`, or a lower `--max-interval`), not a rewrite. A card failing in `1-3d` as well was never encoded, and no scheduler change will save it. Do not rewrite a card whose only failures are at long intervals; you will be rewriting a card that works.
- **`failedAtIntervals`** per card is the same test at card resolution.
- **`byHour`** is a claim about the reviewer rather than the material. Treat it as a hypothesis to raise with them, never as grounds to edit cards.

**Respect the `warning` key.** Below a few hundred graded reviews these rates are noise. If `history` emits a warning, say so in your reply instead of reasoning from the percentages; a confident diagnosis off thirty reviews is worse than no diagnosis, because it produces edits to cards that were never the problem.

### 3. Diagnose across cards, not within one

This is the part a model gets wrong by default. The instinct on seeing eight failing cards is to write eight better explanations. Usually that is the wrong move.

Repeated lapses across related cards rarely mean each fact is individually hard. They usually mean two or more facts are **competing**: similar surface form, adjacent meaning, learned at the same time. The learner has not failed to store them, they have failed to keep them apart. Re-explaining each one in isolation reinforces the interference.

So look at the failing set as a set. If two cards keep trading places, the fix is one card that makes the distinction explicit, not two cards that each describe one side.

### 4. Write discriminations, not definitions

A card that asks "what is X" tests recognition of a label. A card that asks how X differs from the thing it keeps being confused with tests the boundary, which is what actually failed.

Prefer:

> An IPS sits in-line and can block traffic, whereas an IDS sits out-of-band on a tap or SPAN and can only alert.

over two separate cards defining IPS and IDS.

Other rules that hold up:

- **One retrieval per card.** The front asks one thing. The back may be rich.
- **Do not card what was never missed.** A card for a fact already known costs review time forever and buys nothing. `weak` is the input for a reason.
- **Do not bulk-generate.** Ten targeted cards beat two hundred generated from a syllabus, and two hundred cards nobody chose will be abandoned inside a week.
- **Prefer `update` to delete-and-recreate.** A card whose explanation was bad should keep its review history; that history is the evidence of what was hard.

### 5. Write it

`add --file` takes JSON, or `-` for stdin:

```json
[
  {
    "deck": "Chemistry::Organic",
    "notetype": "Basic",
    "fields": {
      "Front": "Why does an SN2 reaction invert stereochemistry but SN1 does not?",
      "Back": "SN2 is a single concerted backside attack, so the nucleophile enters opposite the leaving group and the centre flips. SN1 passes through a planar carbocation, so attack from either face gives a racemic mixture."
    },
    "tags": ["chem/mechanisms", "discrimination"]
  }
]
```

Validation runs before anything is written: unknown deck, unknown note type, and unknown field names all fail with the valid options listed, so a rejected call tells you how to fix it. `canAddNotes` also catches duplicates before the write, which makes the dry run a real check rather than a prediction.

### 6. Know which layer the problem is on

Four layers, and picking the wrong one is the most common way to waste a session.

| The problem is | Fix it with | Not with |
|---|---|---|
| the fact is wrong or badly explained | `update` | `add` (a new card orphans the history) |
| two facts keep swapping | one new discrimination card via `add` | rewriting both originals |
| the card *asks* the wrong thing (answer visible on the front, hint too generous) | `template` | `update`, which cannot see the template |
| it survives short intervals and dies at long ones | `retention` / `limits` | rewriting the card |
| it should not be in rotation at all right now | `suspend` | `reschedule`, and never deletion |
| the card gives away its own answer | `template` + `audio --autoplay off` | rewriting the fields |

That last row is the one people get wrong. A hint printed on the front and a hint played on the front are the same leak, but **automatic delivery is what leaks, not the presence of the material**. `{{hint:Field}}` and a `{{tts}}` tag under `autoplay: off` both put the answer on the front and neither gives it away, because the learner has to reach for it. Reaching for a hint after a failed attempt is worth more than never seeing one.

`template` changes every note of that type at once, so verify the dry run. `reschedule` writes a manual entry into the review log and overrides the scheduler's own estimate, which is usually better informed than yours; the honest uses are spreading a backlog (`--days 3-7`, which randomises so the pile does not simply re-form) and resetting a card that was rewritten so heavily its history describes a different card.

## Safety contract

- **Every mutating command is a dry run until `--apply`.** Run without it, read what it says, then apply. Do not reach for `--apply` on the first call.
- **Always pass `--json`.** Parsing the human-readable tables is exactly the brittle step where models start inventing values.
- **There is no delete command, deliberately.** Suspension is reversible, deletion is not, and an agent should not be able to destroy months of review history in one call. Use `suspend` to take a card out of rotation. If something genuinely must be deleted, the human does it in the Anki GUI where it can be undone.
- **`move` preserves scheduling. `add` does not resurrect it.** If cards are in the wrong place, move them; do not recreate them.
- **Do not touch `collection.anki2` directly**, with sqlite3 or otherwise, even though it is sitting right there. A running Anki holds it open with a write-ahead log and keeps state in memory: a direct write risks corruption, and whatever survives is overwritten on the next flush.

## On model choice

This tool hands a model destructive verbs and a search language that is easy to get subtly wrong. Capability is not a luxury here, for three specific reasons.

**Anki search syntax has traps that fail silently rather than loudly.** A tag written `topic/sub` needs `tag:topic/*`; Anki's `::` hierarchy wildcard matches nothing against it and returns zero results, not an error. `deck:X` includes subdecks unless you exclude them. A model that pattern-matches a plausible-looking query and does not verify the count will confidently move the wrong cards, or none, and report success either way.

**Diagnosis is the actual work, and it is not retrieval.** Noticing that six failing cards share one confusable boundary, rather than being six hard facts, requires holding them all in mind and reasoning about interference. A weaker model reliably defaults to re-explaining each card in turn. That output looks like tutoring and does not teach.

**Fabrication is unusually expensive here.** A wrong fact in a chat reply is read once and forgotten. A wrong fact written into a card is rehearsed on a spaced schedule, deliberately, for months. Spaced repetition is exactly as good at installing errors as facts.

Use a frontier model, give it `--json`, and read the dry run.

## Provenance

This tool was designed and written by [Claude Opus 5](https://www.anthropic.com/claude) working with its author, in the course of debugging a real collection: 38 Security+ cards had been sitting in a language deck for weeks because an import header was missing a `#deck:` line.

Two things in here came from that session rather than from planning, and both are worth knowing about:

The `audit` command originally hardcoded the author's deck names. Running the generic rewrite against the real collection immediately produced four false positives, because a cross-cutting priority tag looked 99% owned by the largest deck purely on base rate. The consensus rule that fixes it (a note is misfiled only when *none* of its labels vouches for where it sits) exists because the first design was tested and failed, not because anyone predicted the problem.

The port troubleshooting in the README exists for the same reason. AnkiConnect reported "port in use" when nothing was listening; the real cause was a Windows reserved port range, which reports as permission denied rather than address-in-use.
