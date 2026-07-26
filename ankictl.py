#!/usr/bin/env python3
"""ankictl - let an AI tutor read and write your Anki collection.

A single-file, dependency-free bridge between a language model and a running
Anki, over the AnkiConnect addon. Anki is a near-perfect substrate for a tutor:
it already knows, per fact, whether you know it. This exposes that record so a
model can teach against evidence instead of guesswork, and write what it teaches
back as cards.

The teaching loop it is built for:

    weak     ->  what is the learner actually failing, and how badly
    (model reasons about WHY those specific items are confusable)
    add      ->  write targeted cards, usually discriminations, not definitions
    update   ->  rewrite a card whose back explains the fact badly
    audit    ->  keep the collection structurally sound as it grows

Stdlib only. Requires the AnkiConnect addon (code 2055492159) installed and
Anki running. Nothing here touches collection.anki2 directly: writing to the
file while Anki holds it open risks corruption and gets overwritten anyway.

Reading
  ping                                  confirm Anki + AnkiConnect are up
  decks                                 deck tree with card counts
  weak [query]                          what the learner keeps failing
  history [query]                       the review log: WHEN and WHERE it fails
  find "<anki search>"                  list matching notes
  audit [query]                         find cards sitting in the wrong deck
  fields --notetype "<name>"            list fields
  template --notetype "<name>"          card templates and styling
  tags [query]                          tags in use, with counts
  retention --deck D                    FSRS / scheduler tuning for a preset
  stats                                 collection shape, for orientation

Writing (DRY RUN unless --apply)
  add --file notes.json                 create notes; '-' reads stdin
  update --file edits.json              rewrite fields of existing notes
  move "<anki search>" --to "<deck>"    change deck, scheduling preserved
  suspend / unsuspend "<anki search>"   take cards out of / back into rotation
  limits --deck D [--new N --rev N]     daily limits
  preset --deck D [D2...] --clone NAME  give decks their own options preset
  addfield --notetype N --field F       append a field to a note type
  template --notetype N --set-css F     rewrite a template or its styling
  tags Q --add T / --remove T           tag maintenance; --rename is collection-wide
  reschedule Q --days N                 move due dates; --forget resets to new
  retention --deck D --target 0.9       desired retention on that deck's preset

Every command takes --json for machine-readable output, which is what an agent
should use. Anki search syntax is the same as the Browse bar: deck:Spanish,
tag:chem::*, note:Cloze, -deck:Spanish::*, "deck:Spanish -note:Basic".

Port: tries 127.0.0.1:8765, then whatever port the installed AnkiConnect addon is
actually configured for (read out of its meta.json). ANKI_CONNECT_URL overrides
both and disables the fallback. See the reserved-range note in the README.

See AGENTS.md for the usage contract an AI should follow.
"""

import argparse
import json
import os
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime

# AnkiConnect's default port. On Windows, Hyper-V/WSL/Docker reserve large TCP
# blocks at boot and 8765 sits at the top of a commonly-reserved one (8666-8765).
# A bind into a reserved range fails with WSAEACCES 10013, which AnkiConnect
# reports as "port in use" even though nothing is listening, so the usual fix is
# to move the addon to another port. Rather than make every caller remember that,
# discover_url() reads the port back out of the addon's own config.
DEFAULT_URL = "http://127.0.0.1:8765"
URL = os.environ.get("ANKI_CONNECT_URL") or DEFAULT_URL
PINNED = bool(os.environ.get("ANKI_CONNECT_URL"))  # explicit setting wins, no fallback

ADDON_ID = "2055492159"

TAG_SEPS = ("::", "/")  # Anki's own hierarchy separator, and the common ad-hoc one


class AnkiDown(Exception):
    pass


def addon_data_dirs():
    """Where Anki keeps addons21, per platform."""
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
    elif sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME", os.path.join(home, ".local", "share"))
    return [os.path.join(base, "Anki2", "addons21", ADDON_ID)]


def configured_port():
    """The port AnkiConnect is ACTUALLY on, from its own config.

    meta.json holds the live, user-edited config; config.json holds only the
    shipped defaults, so meta wins. Returns None if the addon is not installed.
    """
    for d in addon_data_dirs():
        for name in ("meta.json", "config.json"):
            path = os.path.join(d, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            conf = data.get("config", data)  # meta.json nests it, config.json does not
            port = conf.get("webBindPort")
            if port:
                return int(port)
    return None


def _post(url, action, params):
    payload = json.dumps({"action": action, "version": 6, "params": params}).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def call(action, **params):
    global URL
    try:
        body = _post(URL, action, params)
    except urllib.error.URLError as e:
        found = None if PINNED else configured_port()
        if found and f":{found}" not in URL:
            # The addon is installed but listening somewhere else. Retry there
            # once and pin it for the rest of the run.
            candidate = f"http://127.0.0.1:{found}"
            try:
                body = _post(candidate, action, params)
            except urllib.error.URLError:
                raise AnkiDown(down_message(e.reason, also_tried=candidate))
            URL = candidate
        else:
            raise AnkiDown(down_message(e.reason))
    if body.get("error"):
        sys.exit(f"AnkiConnect error on '{action}': {body['error']}")
    return body["result"]


def down_message(reason, also_tried=None):
    extra = f"\n  (also tried {also_tried}, the port its config names)" if also_tried else ""
    return (
        f"no AnkiConnect on {URL} ({reason}).{extra}\n"
        "  1. Is Anki running?\n"
        "  2. Addon installed? Tools > Add-ons > Get Add-ons > 2055492159, then restart.\n"
        "  3. Did AnkiConnect say 'Failed to listen on port'? The port is probably in a\n"
        "     Windows reserved range, not genuinely in use. Check with:\n"
        "         netsh interface ipv4 show excludedportrange protocol=tcp\n"
        "     Pick a port outside every listed range, set it in the addon config\n"
        "     (Tools > Add-ons > AnkiConnect > Config, \"webBindPort\"), restart Anki.\n"
        "     This tool reads that setting back automatically; ANKI_CONNECT_URL only\n"
        "     needs setting for a non-local or non-standard host."
    )


def notes_for(query):
    """Resolve an Anki search to note dicts, each annotated with its decks."""
    note_ids = call("findNotes", query=query)
    if not note_ids:
        return []
    notes = call("notesInfo", notes=note_ids)
    card_ids = [cid for n in notes for cid in n["cards"]]
    decks_by_card = call("getDecks", cards=card_ids) if card_ids else {}
    card_to_deck = {cid: d for d, cids in decks_by_card.items() for cid in cids}
    for n in notes:
        n["decks"] = sorted({card_to_deck.get(c, "?") for c in n["cards"]})
    return notes


def first_field(note):
    order = sorted(note["fields"].items(), key=lambda kv: kv[1]["order"])
    return order[0][1]["value"] if order else ""


def tag_root(tag):
    """'chem/organic' -> 'chem'.  'bio::ch5' -> 'bio'."""
    for sep in TAG_SEPS:
        if sep in tag:
            return tag.split(sep, 1)[0]
    return tag


def deck_root(deck):
    """'Chemistry::Organic' -> 'Chemistry'. Misfiles are almost always cross-tree."""
    return deck.split("::", 1)[0]


def signals(note):
    """The labels that claim a note. A note in the wrong deck disagrees with
    where the rest of its label-mates live."""
    out = [("notetype", note["modelName"])]
    out += [("tag", tag_root(t)) for t in note["tags"]]
    return out


def quote(s):
    return f'"{s}"' if " " in s or "+" in s else s


def emit(payload):
    """Machine-readable output. Agents should always pass --json: parsing the
    human tables is exactly the kind of brittle step that invents facts."""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def strip_html(s):
    out, depth = [], 0
    for ch in s:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return (" ".join("".join(out).split())
            .replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">"))


def load_json_arg(path):
    raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"input is not valid JSON: {e}")
    return data if isinstance(data, list) else [data]


# --- commands ---------------------------------------------------------------

def cmd_weak(args):
    """The teaching signal.

    Anki's review log already contains a per-fact record of what this learner
    does not know: lapses (times a mature card was forgotten), ease factor (how
    hard the scheduler has concluded a card is), and the leech tag. That is a
    far better basis for a tutor than asking someone what they find difficult,
    because self-assessment is exactly what spaced repetition exists to correct.

    Cards are ranked by lapses first, then by ease. A model should read the
    OUTPUT of this, look for what the failing items have in common, and teach
    that, rather than re-explaining each card in isolation.
    """
    ids = call("findCards", query=f"({args.query}) -is:new")
    if not ids:
        sys.exit(f"no reviewed cards match {args.query!r}")
    info = call("cardsInfo", cards=ids)
    rows = []
    for c in info:
        if c["lapses"] < args.min_lapses:
            continue
        flds = sorted(c["fields"].items(), key=lambda kv: kv[1]["order"])
        rows.append({
            "cardId": c["cardId"], "noteId": c["note"], "deck": c["deckName"],
            "notetype": c["modelName"], "lapses": c["lapses"], "reps": c["reps"],
            # factor is per-mille (2500 == 250% ease). Below ~2000 means the
            # scheduler has repeatedly been told this card is hard.
            "ease": round(c["factor"] / 10) if c["factor"] else None,
            "intervalDays": c["interval"],
            "fields": {k: strip_html(v["value"]) for k, v in flds},
        })
    rows.sort(key=lambda r: (-r["lapses"], r["ease"] or 9999))
    rows = rows[: args.limit]
    if args.json:
        return emit({"query": args.query, "count": len(rows), "cards": rows})
    if not rows:
        print(f"nothing with {args.min_lapses}+ lapses in {args.query!r}. "
              "Either it is genuinely solid or it has not been reviewed enough yet.")
        return
    print(f"{len(rows)} struggling card(s), worst first:\n")
    for r in rows:
        ease = f"{r['ease']}%" if r["ease"] else "n/a"
        print(f"  lapses={r['lapses']:<3} ease={ease:<6} ivl={r['intervalDays']}d  "
              f"[{r['deck']}]")
        for k, v in list(r["fields"].items())[:2]:
            print(f"    {k}: {v[:100]}")
    print("\nLook for what these have in common before explaining them one by one.\n"
          "Repeated lapses usually mean two facts are competing, not that one is hard.")


def cmd_add(args):
    """Create notes from JSON. This is how a tutor writes what it taught.

    [{"deck": "...", "notetype": "Basic",
      "fields": {"Front": "...", "Back": "..."}, "tags": ["..."]}]
    """
    items = load_json_arg(args.file)
    decks, models = set(call("deckNames")), set(call("modelNames"))
    notes = []
    for i, it in enumerate(items):
        for key in ("deck", "notetype", "fields"):
            if key not in it:
                sys.exit(f"item {i}: missing required key {key!r}")
        if it["deck"] not in decks:
            sys.exit(f"item {i}: deck {it['deck']!r} does not exist. Existing: "
                     f"{sorted(decks)}")
        if it["notetype"] not in models:
            sys.exit(f"item {i}: notetype {it['notetype']!r} does not exist. "
                     f"Existing: {sorted(models)}")
        valid = call("modelFieldNames", modelName=it["notetype"])
        unknown = set(it["fields"]) - set(valid)
        if unknown:
            sys.exit(f"item {i}: notetype {it['notetype']!r} has no field(s) "
                     f"{sorted(unknown)}. Fields are: {valid}")
        notes.append({"deckName": it["deck"], "modelName": it["notetype"],
                      "fields": it["fields"], "tags": it.get("tags", []),
                      "options": {"allowDuplicate": False}})
    # canAddNotes catches duplicates and empty first fields BEFORE writing, so a
    # dry run is a real check rather than a guess about what would happen.
    ok = call("canAddNotes", notes=notes)
    blocked = [i for i, good in enumerate(ok) if not good]
    if not args.apply:
        if args.json:
            return emit({"dryRun": True, "wouldAdd": sum(ok), "blocked": blocked,
                         "reason": "duplicate first field or empty first field"})
        print(f"{sum(ok)} of {len(notes)} note(s) can be added"
              + (f"; {len(blocked)} blocked (duplicate or empty first field): "
                 f"{blocked}" if blocked else ""))
        print("DRY RUN. re-run with --apply.")
        return
    # Send ONLY what canAddNotes accepted. addNotes fails the WHOLE batch if any
    # single note is a duplicate (it returns a top-level error, not per-note
    # nulls), so passing the rejects through means one already-imported card
    # blocks every new one. That makes re-running a capture file, which is the
    # normal way to catch up on what was missed, silently add nothing.
    addable = [n for n, good in zip(notes, ok) if good]
    if not addable:
        msg = f"nothing to add: all {len(notes)} note(s) are already in the collection."
        return emit({"added": 0, "alreadyPresent": len(blocked)}) if args.json \
            else print(msg)
    if not args.json:
        if blocked:
            print(f"{len(blocked)} note(s) already present, skipping those.")
        print(f"adding {len(addable)} new note(s)...")
    ids = call("addNotes", notes=addable)
    added = [i for i in ids if i]
    if args.json:
        return emit({"added": len(added), "noteIds": added,
                     "alreadyPresent": len(blocked)})
    print(f"added {len(added)} note(s).")


def cmd_update(args):
    """Rewrite fields on existing notes: [{"noteId": 123, "fields": {...}}]

    Preferred over delete-and-recreate, which throws away the card's review
    history. A card whose explanation was bad should keep its lapse record.
    """
    items = load_json_arg(args.file)
    for i, it in enumerate(items):
        if "noteId" not in it or "fields" not in it:
            sys.exit(f"item {i}: needs 'noteId' and 'fields'")
    if not args.apply:
        if args.json:
            return emit({"dryRun": True, "wouldUpdate": len(items)})
        for it in items:
            print(f"  note {it['noteId']}: {list(it['fields'])}")
        print(f"\nDRY RUN. {len(items)} note(s). re-run with --apply. "
              "Review history is preserved.")
        return
    for it in items:
        call("updateNoteFields",
             note={"id": it["noteId"], "fields": it["fields"]})
    (emit({"updated": len(items)}) if args.json
     else print(f"updated {len(items)} note(s)."))


def cmd_suspend(args):
    ids = call("findCards", query=args.query)
    if not ids:
        sys.exit(f"nothing matches {args.query!r}")
    verb = "suspend" if args.cmd == "suspend" else "unsuspend"
    if not args.apply:
        if args.json:
            return emit({"dryRun": True, "action": verb, "cards": len(ids)})
        print(f"would {verb} {len(ids)} card(s). DRY RUN, re-run with --apply.")
        return
    call(verb, cards=ids)
    (emit({"action": verb, "cards": len(ids)}) if args.json
     else print(f"{verb}ed {len(ids)} card(s)."))


def cmd_stats(args):
    """Orientation. An agent should call this first: it is cheaper than
    guessing deck and note type names and then failing on a write."""
    decks = call("deckNames")
    models = call("modelNames")
    out = {
        "decks": decks,
        "notetypes": {m: call("modelFieldNames", modelName=m) for m in models},
        "counts": {
            "notes": len(call("findNotes", query="deck:*")),
            "new": len(call("findCards", query="is:new")),
            "due": len(call("findCards", query="is:due")),
            "suspended": len(call("findCards", query="is:suspended")),
            "leeches": len(call("findCards", query="tag:leech")),
        },
    }
    if args.json:
        return emit(out)
    print(f"decks ({len(decks)}): {', '.join(decks)}\n")
    print("notetypes:")
    for m, f in out["notetypes"].items():
        print(f"  {m}: {f}")
    print("\ncounts:", ", ".join(f"{k}={v}" for k, v in out["counts"].items()))


def cmd_ping(_):
    print(f"AnkiConnect v{call('version')}")
    print(f"decks: {len(call('deckNames'))}  notes: {len(call('findNotes', query='deck:*'))}")


def cmd_decks(args):
    stats = call("getDeckStats", decks=call("deckNames"))
    rows = sorted(stats.values(), key=lambda d: d["name"])
    if args.json:
        return emit({"decks": [
            {"name": d["name"], "total": d["total_in_deck"], "new": d["new_count"],
             "learn": d["learn_count"], "due": d["review_count"]} for d in rows]})
    print(f"{'deck':<32} {'total':>7} {'new':>6} {'learn':>6} {'due':>6}")
    for d in rows:
        print(f"{d['name']:<32} {d['total_in_deck']:>7} {d['new_count']:>6} "
              f"{d['learn_count']:>6} {d['review_count']:>6}")


def cmd_audit(args):
    """Find cards sitting in the wrong deck, with no configuration.

    The insight: a note type or a tag root is a claim about what a note IS, and
    the deck is a claim about where it LIVES. In a healthy collection those two
    agree almost perfectly. So learn each label's home deck from the collection
    itself (whichever deck holds the overwhelming majority of that label's
    notes), then report the stragglers. Nothing is hardcoded, so this works on
    any collection, and it adapts as decks are renamed or added.
    """
    notes = notes_for(args.query)
    if not notes:
        sys.exit(f"no notes match {args.query!r}")

    # label -> deck_root -> count
    tally = {}
    for n in notes:
        for label in signals(n):
            per_deck = tally.setdefault(label, {})
            for d in {deck_root(x) for x in n["decks"]}:
                per_deck[d] = per_deck.get(d, 0) + 1

    home = {}          # label -> (deck_root, share)
    for label, per_deck in tally.items():
        total = sum(per_deck.values())
        if total < args.min_notes:
            continue   # too little evidence to call anything an outlier
        top, count = max(per_deck.items(), key=lambda kv: kv[1])
        share = count / total
        if share >= args.min_share and count > 1:
            home[label] = (top, share)

    # Consensus, and it is what makes this usable. Some labels are orthogonal to
    # subject: a priority tag, 'trap', 'leech', or a shared note type spreads
    # across every deck, so it looks like it "belongs" to whichever deck is
    # biggest and would indict every note elsewhere. So a note is only misfiled
    # when NONE of its labels vouches for the deck it is actually in. One label
    # dissenting while another agrees is a cross-cutting tag, not a misfile.
    flagged = {}
    for n in notes:
        opinions = [(lbl, *home[lbl]) for lbl in signals(n) if lbl in home]
        if not opinions:
            continue
        here = {deck_root(d) for d in n["decks"]}
        if any(want in here for _, want, _ in opinions):
            continue   # something vouches for where it sits
        label, want, share = max(opinions, key=lambda o: o[2])
        flagged[n["noteId"]] = (n, sorted(n["decks"])[0], want, share, label)

    if args.json:
        return emit({"checked": len(notes), "labelsWithHome": len(home),
                     "misfiled": [
                         {"noteId": n["noteId"], "in": deck, "expected": want,
                          "because": {"label": f"{lk}:{lv}", "share": round(share, 3)},
                          "notetype": n["modelName"],
                          "preview": strip_html(first_field(n))[:120]}
                         for n, deck, want, share, (lk, lv) in flagged.values()]})
    if not flagged:
        print(f"clean: no note sits apart from its label-mates "
              f"(checked {len(notes)} notes, {len(home)} labels with a clear home).")
        return

    print(f"{len(flagged)} misfiled note(s) out of {len(notes)} checked:\n")
    fixes = {}
    for n, deck, want, share, (kind, label) in sorted(
            flagged.values(), key=lambda f: (-f[3], f[1])):
        print(f"  in '{deck}' -> expected under '{want}'   "
              f"({kind} '{label}' is {share:.0%} in '{want}')")
        print(f"    [{n['modelName']}] {first_field(n)[:100]}")
        sel = (f"{kind}:{label}*" if kind == "tag" else f"note:{label}")
        fixes.setdefault((deck, want, sel), 0)
        fixes[(deck, want, sel)] += 1

    print("\nsuggested fixes (dry run without --apply):")
    for (deck, want, sel), cnt in fixes.items():
        print(f"  ankictl.py move {quote(f'deck:{deck} -deck:{deck}::* {sel}')} "
              f"--to {quote(want)}    # {cnt} note(s)")
    print("\nCheck each search in Anki's Browse bar first. Note that a tag written\n"
          "'topic/sub' needs 'tag:topic/*', not Anki's '::' hierarchy wildcard.")


def cmd_find(args):
    notes = notes_for(args.query)
    if args.json:
        return emit({"query": args.query, "count": len(notes), "notes": [
            {"noteId": n["noteId"], "notetype": n["modelName"],
             "decks": n["decks"], "tags": n["tags"],
             "fields": {k: strip_html(v["value"])
                        for k, v in sorted(n["fields"].items(),
                                           key=lambda kv: kv[1]["order"])}}
            for n in notes[: args.limit]]})
    print(f"{len(notes)} note(s) matching {args.query!r}\n")
    for n in notes[: args.limit]:
        print(f"  [{n['modelName']}] {'/'.join(n['decks'])}  tags={' '.join(n['tags'])}")
        print(f"    {first_field(n)[:130]}")


def cmd_move(args):
    notes = notes_for(args.query)
    if not notes:
        sys.exit(f"nothing matches {args.query!r}")
    if args.to not in call("deckNames"):
        sys.exit(f"deck {args.to!r} does not exist (create it in Anki first)")
    card_ids = [c for n in notes for c in n["cards"]]
    print(f"{len(notes)} note(s) / {len(card_ids)} card(s) -> '{args.to}'")
    for n in notes[:15]:
        print(f"  {'/'.join(n['decks'])}: {first_field(n)[:90]}")
    if len(notes) > 15:
        print(f"  ... and {len(notes) - 15} more")
    if not args.apply:
        print("\nDRY RUN. re-run with --apply to move. Scheduling is preserved.")
        return
    call("changeDeck", cards=card_ids, deck=args.to)
    print(f"\nmoved {len(card_ids)} card(s).")


def cmd_limits(args):
    conf = call("getDeckConfig", deck=args.deck)
    print(f"deck '{args.deck}' uses preset '{conf['name']}' (id {conf['id']})")
    print(f"  new cards/day     : {conf['new']['perDay']}")
    print(f"  max reviews/day   : {conf['rev']['perDay']}")
    if args.new is None and args.rev is None:
        print("\nnote: the review limit gates new cards under the v3 scheduler, so both\n"
              "must be raised together. 'New Cards Ignore Review Limit' is a collection\n"
              "setting and stays a GUI checkbox (Deck Options > Daily Limits).")
        return
    if conf["id"] == 1 and not args.force:
        sys.exit("refusing: this deck is on the shared 'Default' preset, so the change\n"
                 "would hit every deck. Run 'preset --deck ... --clone ...' first, "
                 "or pass --force.")
    if args.new is not None:
        conf["new"]["perDay"] = args.new
    if args.rev is not None:
        conf["rev"]["perDay"] = args.rev
    print(f"  -> new/day {conf['new']['perDay']}, reviews/day {conf['rev']['perDay']}")
    if not args.apply:
        print("\nDRY RUN. re-run with --apply.")
        return
    call("saveDeckConfig", config=conf)
    print("saved.")


def cmd_preset(args):
    # Under the v3 scheduler a deck is bound by its OWN limits and by every
    # parent's, so a subdeck given a roomy preset while its parent keeps the
    # default is still throttled by the parent. Pass the whole chain.
    current = call("getDeckConfig", deck=args.deck[0])
    print(f"{args.deck} on preset '{current['name']}' -> clone as '{args.clone}'")
    if not args.apply:
        print("DRY RUN. re-run with --apply.")
        return
    new_id = call("cloneDeckConfigId", name=args.clone, cloneFrom=current["id"])
    if new_id is False:
        sys.exit("clone failed")
    call("setDeckConfigId", decks=args.deck, configId=new_id)
    print(f"{len(args.deck)} deck(s) now on preset '{args.clone}' (id {new_id}).")


def cmd_fields(args):
    for i, f in enumerate(call("modelFieldNames", modelName=args.notetype)):
        print(f"  {i + 1}. {f}")


def cmd_addfield(args):
    existing = call("modelFieldNames", modelName=args.notetype)
    if args.field in existing:
        sys.exit(f"'{args.field}' already exists on '{args.notetype}'")
    print(f"append '{args.field}' to '{args.notetype}' (currently {len(existing)} fields)")
    if not args.apply:
        print("DRY RUN. re-run with --apply.")
        return
    call("modelFieldAdd", modelName=args.notetype, fieldName=args.field,
         index=len(existing))
    print("added. Field is empty on existing notes and appears on no template until "
          "you reference it.")


# --- review log -------------------------------------------------------------

# revlog.type. 4 is a manual reschedule (setDueDate / forget), which records no
# recall event at all, so it must be dropped before computing any accuracy.
REV_LEARN, REV_REVIEW, REV_RELEARN, REV_FILTERED, REV_MANUAL = 0, 1, 2, 3, 4
GRADED = (REV_LEARN, REV_REVIEW, REV_RELEARN, REV_FILTERED)

# lastIvl is days when positive and SECONDS when negative (Anki stores sub-day
# learning steps that way). Bucketing on it answers the question a lapse count
# cannot: not "is this card hard" but "how long does it survive".
IVL_BUCKETS = [
    ("learning (<1d)", -10 ** 9, 0),
    ("1-3d", 1, 3),
    ("4-7d", 4, 7),
    ("8-21d", 8, 21),
    ("22-60d", 22, 60),
    ("60d+", 61, 10 ** 9),
]


def bucket_of(last_ivl):
    days = last_ivl if last_ivl >= 0 else 0
    for name, lo, hi in IVL_BUCKETS:
        if lo <= days <= hi:
            return name
    return "60d+"


def rate(again, total):
    return round(again / total, 3) if total else None


def cmd_history(args):
    """Read the review log itself, not the summary counters.

    'weak' reads lapses and ease off the card, which is a compression of this.
    The revlog keeps every individual answer: which button, at what interval,
    at what hour, how long it took. That distinguishes failure modes a lapse
    count flattens together.

    A card failing only past a three-week interval is a consolidation problem
    and wants a shorter maximum interval or a rewrite. A card failing at every
    interval was never encoded and wants a better card. A card that fails only
    late at night is not a card problem at all. All three look identical as
    'lapses=4'.
    """
    ids = call("findCards", query=f"({args.query}) -is:new")
    if not ids:
        sys.exit(f"no reviewed cards match {args.query!r}")
    logs = call("getReviewsOfCards", cards=ids)

    entries = []  # (cardId, revlog entry)
    for cid, revs in logs.items():
        for r in revs:
            if r["type"] in GRADED and r["ease"] > 0:
                entries.append((int(cid), r))
    if not entries:
        sys.exit(f"{len(ids)} card(s) matched but the log holds no graded reviews "
                 "(manual reschedules only).")

    by_hour, by_bucket, per_card = {}, {}, {}
    for cid, r in entries:
        again = r["ease"] == 1
        hour = datetime.fromtimestamp(r["id"] / 1000).hour
        for key, table in ((hour, by_hour), (bucket_of(r["lastIvl"]), by_bucket)):
            slot = table.setdefault(key, {"reviews": 0, "again": 0})
            slot["reviews"] += 1
            slot["again"] += again
        c = per_card.setdefault(cid, {"reviews": 0, "again": 0, "ms": [],
                                      "failedAt": []})
        c["reviews"] += 1
        c["again"] += again
        c["ms"].append(r["time"])
        if again and r["lastIvl"] > 0:
            c["failedAt"].append(r["lastIvl"])

    stamps = [r["id"] / 1000 for _, r in entries]
    summary = {
        "reviews": len(entries),
        "cards": len(per_card),
        "againRate": rate(sum(1 for _, r in entries if r["ease"] == 1), len(entries)),
        "medianSeconds": round(statistics.median(r["time"] for _, r in entries) / 1000, 1),
        "firstReview": datetime.fromtimestamp(min(stamps)).strftime("%Y-%m-%d"),
        "lastReview": datetime.fromtimestamp(max(stamps)).strftime("%Y-%m-%d"),
    }

    worst = [cid for cid, c in per_card.items()
             if c["reviews"] >= args.min_reps and c["again"]]
    worst.sort(key=lambda cid: (-per_card[cid]["again"] / per_card[cid]["reviews"],
                                -per_card[cid]["reviews"]))
    worst = worst[: args.limit]
    detail = {c["cardId"]: c for c in call("cardsInfo", cards=worst)} if worst else {}

    cards_out = []
    for cid in worst:
        c, d = per_card[cid], detail.get(cid, {})
        flds = sorted(d.get("fields", {}).items(), key=lambda kv: kv[1]["order"])
        cards_out.append({
            "cardId": cid, "deck": d.get("deckName"),
            "reviews": c["reviews"], "again": c["again"],
            "againRate": rate(c["again"], c["reviews"]),
            "medianSeconds": round(statistics.median(c["ms"]) / 1000, 1),
            # the intervals it survived to and then failed at. A tight cluster
            # is a consolidation ceiling; scattered values are a bad card.
            "failedAtIntervals": sorted(c["failedAt"]),
            "fields": {k: strip_html(v["value"])[:160] for k, v in flds[:2]},
        })

    out = {
        "query": args.query,
        "summary": summary,
        "byInterval": {name: {**v, "againRate": rate(v["again"], v["reviews"])}
                       for name, _, _ in IVL_BUCKETS if (v := by_bucket.get(name))},
        "byHour": {f"{h:02d}": {**v, "againRate": rate(v["again"], v["reviews"])}
                   for h, v in sorted(by_hour.items())},
        "cards": cards_out,
    }
    if summary["reviews"] < args.min_sample:
        out["warning"] = (
            f"only {summary['reviews']} graded reviews. Rates over a sample this "
            "small are noise; treat the breakdowns as descriptive, not as evidence. "
            "Come back after a few hundred.")
    if args.json:
        return emit(out)

    s = summary
    print(f"{s['reviews']} graded reviews over {s['cards']} card(s), "
          f"{s['firstReview']} to {s['lastReview']}")
    print(f"again rate {s['againRate']:.0%}, median {s['medianSeconds']}s per answer\n")
    print("by interval the card had reached:")
    for name, v in out["byInterval"].items():
        print(f"  {name:<16} {v['reviews']:>5} reviews   again {v['againRate']:.0%}")
    print("\nby hour of day:")
    for h, v in out["byHour"].items():
        print(f"  {h}:00{'':<12} {v['reviews']:>5} reviews   again {v['againRate']:.0%}")
    if cards_out:
        print(f"\nworst {len(cards_out)} card(s) with {args.min_reps}+ reviews:")
        for c in cards_out:
            failed = (f"  failed at {c['failedAtIntervals']}d"
                      if c["failedAtIntervals"] else "")
            print(f"  again {c['againRate']:.0%} of {c['reviews']}   "
                  f"{c['medianSeconds']}s   [{c['deck']}]{failed}")
            for k, v in c["fields"].items():
                print(f"    {k}: {v[:100]}")
    if "warning" in out:
        print(f"\nNOTE: {out['warning']}")


# --- card templates ---------------------------------------------------------

def cmd_template(args):
    """Read or rewrite a note type's card templates and styling.

    This is the layer 'add' and 'update' cannot reach. Those change what a card
    SAYS; this changes what it ASKS. A note whose fields are all correct can
    still be a bad card because the template shows the answer on the front, or
    renders a hint that makes recall unnecessary.
    """
    templates = call("modelTemplates", modelName=args.notetype)
    css = call("modelStyling", modelName=args.notetype)["css"]

    writes = {k: v for k, v in (("front", args.set_front), ("back", args.set_back),
                                ("css", args.set_css)) if v}
    if not writes:
        if args.json:
            return emit({"notetype": args.notetype, "templates": templates, "css": css})
        for name, sides in templates.items():
            print(f"--- card template: {name} ---")
            for side, html in sides.items():
                print(f"  [{side}]")
                for line in html.splitlines():
                    print(f"    {line}")
        print(f"--- styling ({len(css.splitlines())} lines) ---")
        for line in css.splitlines():
            print(f"    {line}")
        return

    if ("front" in writes or "back" in writes) and not args.card:
        if len(templates) != 1:
            sys.exit(f"'{args.notetype}' has {len(templates)} card templates "
                     f"({', '.join(templates)}); pass --card to say which.")
        args.card = next(iter(templates))
    if args.card and args.card not in templates:
        sys.exit(f"no card template {args.card!r} on '{args.notetype}'. "
                 f"Have: {', '.join(templates)}")

    read = lambda p: (sys.stdin.read() if p == "-" else open(p, encoding="utf-8").read())
    new_sides = {}
    if "front" in writes:
        new_sides["Front"] = read(writes["front"])
    if "back" in writes:
        new_sides["Back"] = read(writes["back"])
    new_css = read(writes["css"]) if "css" in writes else None

    # A field name mistyped in a template does not error, it renders literally
    # and silently produces a card that asks nothing. Check before writing.
    known = set(call("modelFieldNames", modelName=args.notetype))
    for side, html in new_sides.items():
        refs = {tok.split("}}")[0].strip().split(":")[-1]
        for tok in html.split("{{")[1:] if "}}" in tok}
        unknown = {r for r in refs if r and not r.startswith(("#", "/", "^"))
                   and r not in known and r not in ("FrontSide", "Tags", "Type",
                                                    "Deck", "Subdeck", "Card")}
        if unknown:
            sys.exit(f"{side} references unknown field(s) {sorted(unknown)}. "
                     f"Fields on '{args.notetype}': {sorted(known)}")

    print(f"'{args.notetype}'"
          + (f" card '{args.card}': {', '.join(new_sides)}" if new_sides else "")
          + (f"{' +' if new_sides else ':'} styling ({len(new_css.splitlines())} lines)"
             if new_css is not None else ""))
    if not args.apply:
        print("DRY RUN. re-run with --apply. This rewrites the template for EVERY "
              "note of this type; review history is untouched.")
        return
    if new_sides:
        merged = dict(templates[args.card])
        merged.update(new_sides)
        call("updateModelTemplates",
             model={"name": args.notetype, "templates": {args.card: merged}})
    if new_css is not None:
        call("updateModelStyling", model={"name": args.notetype, "css": new_css})
    print("saved.")


# --- tags -------------------------------------------------------------------

def cmd_tags(args):
    """Tags are the retrieval index a tutor navigates by. Keep them clean."""
    if args.rename:
        old, new = args.rename
        print(f"rename tag '{old}' -> '{new}' across the whole collection")
        if not args.apply:
            print("DRY RUN. re-run with --apply. This ignores any query you passed.")
            return
        call("replaceTagsInAllNotes", tag_to_replace=old, replace_with_tag=new)
        print("renamed.")
        return

    notes = notes_for(args.query)
    if not args.add and not args.remove:
        counts = {}
        for n in notes:
            for t in n["tags"]:
                counts[t] = counts.get(t, 0) + 1
        rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        untagged = sum(1 for n in notes if not n["tags"])
        if args.json:
            return emit({"query": args.query, "notes": len(notes),
                         "untagged": untagged, "tags": dict(rows)})
        print(f"{len(rows)} tag(s) across {len(notes)} note(s), "
              f"{untagged} untagged\n")
        for t, c in rows:
            print(f"  {c:>5}  {t}")
        return

    if not notes:
        sys.exit(f"nothing matches {args.query!r}")
    ids = [n["noteId"] for n in notes]
    verb = "add" if args.add else "remove"
    tags = args.add or args.remove
    print(f"{verb} {tags} on {len(ids)} note(s) matching {args.query!r}")
    for n in notes[:10]:
        print(f"  {first_field(n)[:90]}")
    if len(notes) > 10:
        print(f"  ... and {len(notes) - 10} more")
    if not args.apply:
        print("\nDRY RUN. re-run with --apply.")
        return
    call("addTags" if args.add else "removeTags", notes=ids, tags=" ".join(tags))
    print(f"\n{verb}d on {len(ids)} note(s).")


# --- scheduling -------------------------------------------------------------

def cmd_reschedule(args):
    """Move due dates, or send cards back to the new queue.

    Use sparingly and prefer 'suspend'. Rescheduling writes a manual entry into
    the revlog and overrides the scheduler's own estimate, which is usually
    better informed than a guess. The honest uses are: a backlog that needs
    spreading, and a card whose explanation was rewritten badly enough that its
    old history no longer describes the card that now exists.
    """
    if not args.forget and not args.days:
        sys.exit("pass --days N (or --forget). Doing nothing is the safe default here.")
    ids = call("findCards", query=args.query)
    if not ids:
        sys.exit(f"nothing matches {args.query!r}")
    what = "reset to new (history kept in the log)" if args.forget \
        else f"due in {args.days} day(s)"
    print(f"{len(ids)} card(s) matching {args.query!r} -> {what}")
    if not args.apply:
        print("DRY RUN. re-run with --apply.")
        return
    if args.forget:
        call("forgetCards", cards=ids)
    else:
        # setDueDate takes a string: "3" is exactly 3 days out, "3-7" randomises
        # in that range (better for a backlog: it avoids rebuilding the pile),
        # and a trailing '!' keeps the current interval instead of resetting it.
        call("setDueDate", cards=ids, days=str(args.days))
    print(f"rescheduled {len(ids)} card(s).")


def cmd_retention(args):
    """Show, and optionally set, the scheduler tuning on a deck's preset.

    Desired retention is the one FSRS knob worth touching: the fraction of cards
    you want to recall successfully. Raising it shortens every interval, so it
    buys accuracy with review time, steeply. 0.9 is the default for good reason;
    0.95 can roughly double daily load for a few points of recall.
    """
    conf = call("getDeckConfig", deck=args.deck)
    weights = conf.get("fsrsParams6") or conf.get("fsrsParams5") or conf.get("fsrsWeights") or []
    state = {
        "deck": args.deck, "preset": conf["name"], "presetId": conf["id"],
        "fsrsParametersPresent": bool(weights),
        "desiredRetention": conf.get("desiredRetention"),
        "maximumIntervalDays": conf["rev"]["maxIvl"],
        "learningStepsMinutes": conf["new"]["delays"],
        "relearningStepsMinutes": conf["lapse"]["delays"],
        "leechThreshold": conf["lapse"]["leechFails"],
        "newPerDay": conf["new"]["perDay"], "reviewsPerDay": conf["rev"]["perDay"],
    }
    if args.target is None and args.max_interval is None and args.leech is None:
        if args.json:
            return emit(state)
        for k, v in state.items():
            print(f"  {k:<24}: {v}")
        if not weights:
            print("\nNo FSRS parameters on this preset. Either FSRS is off collection-wide,\n"
                  "or it is on but has never been optimised. The master toggle and the\n"
                  "'Optimize' button are GUI-only (Deck Options > FSRS); desired retention\n"
                  "is settable here but does nothing while FSRS is off.")
        return

    if conf["id"] == 1 and not args.force:
        sys.exit("refusing: this deck is on the shared 'Default' preset, so the change\n"
                 "would hit every deck. Run 'preset --deck ... --clone ...' first, "
                 "or pass --force.")
    if args.target is not None:
        if not 0.7 <= args.target <= 0.99:
            sys.exit("desired retention must be between 0.70 and 0.99; Anki rejects "
                     "anything outside that, and above ~0.95 the review cost explodes.")
        conf["desiredRetention"] = args.target
        print(f"  desiredRetention -> {args.target}")
    if args.max_interval is not None:
        conf["rev"]["maxIvl"] = args.max_interval
        print(f"  maximumInterval  -> {args.max_interval}d")
    if args.leech is not None:
        conf["lapse"]["leechFails"] = args.leech
        print(f"  leechThreshold   -> {args.leech}")
    if not args.apply:
        print("DRY RUN. re-run with --apply.")
        return
    call("saveDeckConfig", config=conf)
    print(f"saved to preset '{conf['name']}'.")


def cmd_audio(args):
    """Autoplay and replay behaviour for a deck's preset.

    Turning autoplay OFF is what converts a `{{tts}}` tag from something that
    fires at you into a play button you press. That distinction matters on a
    recognition card: audio that plays by itself on the front hands over the
    pronunciation before any retrieval is attempted, which is the same leak as
    printing the reading on the front. Audio you choose to play is a hint.

    These are per-PRESET, not per-deck, and not per-card, so they apply to
    every deck sharing the preset.
    """
    conf = call("getDeckConfig", deck=args.deck)
    flags = {"autoplay": "play audio automatically",
             "replayq": "replay the question's audio when showing the answer"}
    if args.autoplay is None and args.replay_question is None:
        state = {"deck": args.deck, "preset": conf["name"],
                 **{k: conf.get(k) for k in flags}}
        if args.json:
            return emit(state)
        print(f"deck '{args.deck}' uses preset '{conf['name']}'")
        for k, why in flags.items():
            print(f"  {k:<10}: {conf.get(k)}   ({why})")
        return

    if conf["id"] == 1 and not args.force:
        sys.exit("refusing: this deck is on the shared 'Default' preset, so the change\n"
                 "would hit every deck. Run 'preset --deck ... --clone ...' first, "
                 "or pass --force.")
    on = lambda v: v == "on"
    if args.autoplay is not None:
        conf["autoplay"] = on(args.autoplay)
        print(f"  autoplay -> {conf['autoplay']}")
    if args.replay_question is not None:
        conf["replayq"] = on(args.replay_question)
        print(f"  replayq  -> {conf['replayq']}")
    if not args.apply:
        print("DRY RUN. re-run with --apply.")
        return
    call("saveDeckConfig", config=conf)
    print(f"saved to preset '{conf['name']}'.")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping").set_defaults(fn=cmd_ping)
    sub.add_parser("decks").set_defaults(fn=cmd_decks)
    a = sub.add_parser("audit")
    a.add_argument("query", nargs="?", default="deck:*",
                   help="limit the audit to a subset (default: whole collection)")
    a.add_argument("--min-share", type=float, default=0.9,
                   help="how dominant a label's home deck must be to judge "
                        "outliers (default 0.9). Lower finds more, and more noise.")
    a.add_argument("--min-notes", type=int, default=5,
                   help="ignore labels with fewer notes than this (default 5)")
    a.set_defaults(fn=cmd_audit)

    f = sub.add_parser("find")
    f.add_argument("query")
    f.add_argument("--limit", type=int, default=40)
    f.set_defaults(fn=cmd_find)

    m = sub.add_parser("move")
    m.add_argument("query")
    m.add_argument("--to", required=True)
    m.add_argument("--apply", action="store_true")
    m.set_defaults(fn=cmd_move)

    l = sub.add_parser("limits")
    l.add_argument("--deck", required=True)
    l.add_argument("--new", type=int)
    l.add_argument("--rev", type=int)
    l.add_argument("--apply", action="store_true")
    l.add_argument("--force", action="store_true",
                   help="allow editing the shared Default preset")
    l.set_defaults(fn=cmd_limits)

    pr = sub.add_parser("preset")
    pr.add_argument("--deck", required=True, nargs="+",
                    help="one or more decks; pass the parent chain too")
    pr.add_argument("--clone", required=True)
    pr.add_argument("--apply", action="store_true")
    pr.set_defaults(fn=cmd_preset)

    fl = sub.add_parser("fields")
    fl.add_argument("--notetype", required=True)
    fl.set_defaults(fn=cmd_fields)

    af = sub.add_parser("addfield")
    af.add_argument("--notetype", required=True)
    af.add_argument("--field", required=True)
    af.add_argument("--apply", action="store_true")
    af.set_defaults(fn=cmd_addfield)

    h = sub.add_parser("history", help="the review log: when and where it fails")
    h.add_argument("query", nargs="?", default="deck:*")
    h.add_argument("--min-reps", type=int, default=3,
                   help="ignore cards with fewer reviews than this (default 3)")
    h.add_argument("--limit", type=int, default=20)
    h.add_argument("--min-sample", type=int, default=200,
                   help="warn that the rates are noise below this many reviews")
    h.set_defaults(fn=cmd_history)

    tp = sub.add_parser("template", help="card templates and styling")
    tp.add_argument("--notetype", required=True)
    tp.add_argument("--card", help="which card template (needed if the type has several)")
    tp.add_argument("--set-front", metavar="FILE", help="HTML file, or '-' for stdin")
    tp.add_argument("--set-back", metavar="FILE")
    tp.add_argument("--set-css", metavar="FILE")
    tp.add_argument("--apply", action="store_true")
    tp.set_defaults(fn=cmd_template)

    tg = sub.add_parser("tags", help="tags in use, and tag maintenance")
    tg.add_argument("query", nargs="?", default="deck:*")
    tg.add_argument("--add", nargs="+", metavar="TAG")
    tg.add_argument("--remove", nargs="+", metavar="TAG")
    tg.add_argument("--rename", nargs=2, metavar=("OLD", "NEW"),
                    help="collection-wide; ignores the query")
    tg.add_argument("--apply", action="store_true")
    tg.set_defaults(fn=cmd_tags)

    rs = sub.add_parser("reschedule", help="move due dates; prefer suspend")
    rs.add_argument("query")
    rs.add_argument("--days", help="'3', or '3-7' to spread a backlog randomly")
    rs.add_argument("--forget", action="store_true", help="send back to the new queue")
    rs.add_argument("--apply", action="store_true")
    rs.set_defaults(fn=cmd_reschedule)

    au = sub.add_parser("audio", help="autoplay / replay behaviour on a deck's preset")
    au.add_argument("--deck", required=True)
    au.add_argument("--autoplay", choices=("on", "off"),
                    help="off turns every {{tts}} and [sound:] into a play button")
    au.add_argument("--replay-question", choices=("on", "off"))
    au.add_argument("--apply", action="store_true")
    au.add_argument("--force", action="store_true",
                    help="allow editing the shared Default preset")
    au.set_defaults(fn=cmd_audio)

    rt = sub.add_parser("retention", help="scheduler tuning on a deck's preset")
    rt.add_argument("--deck", required=True)
    rt.add_argument("--target", type=float, metavar="0.9",
                    help="FSRS desired retention, 0.70-0.99")
    rt.add_argument("--max-interval", type=int, metavar="DAYS")
    rt.add_argument("--leech", type=int, metavar="N", help="lapses before leech")
    rt.add_argument("--apply", action="store_true")
    rt.add_argument("--force", action="store_true",
                    help="allow editing the shared Default preset")
    rt.set_defaults(fn=cmd_retention)

    w = sub.add_parser("weak", help="what the learner keeps failing")
    w.add_argument("query", nargs="?", default="deck:*")
    w.add_argument("--min-lapses", type=int, default=1)
    w.add_argument("--limit", type=int, default=30)
    w.set_defaults(fn=cmd_weak)

    ad = sub.add_parser("add", help="create notes from JSON")
    ad.add_argument("--file", required=True, help="JSON file, or '-' for stdin")
    ad.add_argument("--apply", action="store_true")
    ad.set_defaults(fn=cmd_add)

    up = sub.add_parser("update", help="rewrite fields on existing notes")
    up.add_argument("--file", required=True, help="JSON file, or '-' for stdin")
    up.add_argument("--apply", action="store_true")
    up.set_defaults(fn=cmd_update)

    for name in ("suspend", "unsuspend"):
        s = sub.add_parser(name)
        s.add_argument("query")
        s.add_argument("--apply", action="store_true")
        s.set_defaults(fn=cmd_suspend)

    sub.add_parser("stats", help="decks, notetypes, counts").set_defaults(fn=cmd_stats)

    for sp in sub.choices.values():
        sp.add_argument("--json", action="store_true",
                        help="machine-readable output; agents should use this")

    args = p.parse_args()
    try:
        args.fn(args)
    except AnkiDown as e:
        sys.exit(str(e))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
