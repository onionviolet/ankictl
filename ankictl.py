#!/usr/bin/env python3
"""ankictl - talk to a running Anki over AnkiConnect (127.0.0.1:8765).

Stdlib only. Requires the AnkiConnect addon (code 2055492159) installed and
Anki running. Nothing here touches collection.anki2 directly: writing to the
file while Anki holds it open risks corruption and gets overwritten anyway.

Commands
  ping                                  confirm Anki + AnkiConnect are up
  decks                                 deck tree with card counts
  audit                                 find cards sitting in the wrong deck
  find "<anki search>"                  list matching notes
  move "<anki search>" --to "<deck>"    change deck (DRY RUN unless --apply)
  limits --deck "<deck>"                show the deck's daily limits
  limits --deck "<deck>" --new N --rev N [--apply]   set them
  preset --deck "<deck>" --clone "<name>" [--apply]  give a deck its own preset
  fields --notetype "<name>"            list fields
  addfield --notetype "<name>" --field "<name>" [--apply]

Anki search syntax is the same as the Browse bar: deck:Mandarin, tag:sec-plus::*,
note:Cloze, -deck:Mandarin::*, "deck:Mandarin -note:Mandarin*".

Port: defaults to 127.0.0.1:8765. Override with the ANKI_CONNECT_URL env var when
that port is unavailable (see the reserved-range note below and in the README).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# AnkiConnect's port. Override with ANKI_CONNECT_URL when 8765 is unavailable:
# on Windows, Hyper-V/WSL/Docker reserve large TCP blocks at boot, and 8765 sits
# at the top of a commonly-reserved one (8666-8765). A bind into a reserved range
# fails with WSAEACCES 10013, which AnkiConnect reports as "port in use" even
# though nothing is listening. See the troubleshooting note in the README.
URL = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")

# Which notes belong in which deck. Extend as decks are added.
# (deck-name prefix, predicate on the note's notetype + tags)
OWNERSHIP = [
    ("Mandarin", lambda nt, tags: nt.startswith("Mandarin")),
    ("Sec+", lambda nt, tags: any(t.startswith("sec-plus") for t in tags)),
    ("EMT", lambda nt, tags: any(t.startswith("emt") for t in tags)),
    ("CSCI", lambda nt, tags: "csci1100" in tags),
]


class AnkiDown(Exception):
    pass


def call(action, **params):
    payload = json.dumps({"action": action, "version": 6, "params": params}).encode()
    req = urllib.request.Request(URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.load(r)
    except urllib.error.URLError as e:
        raise AnkiDown(
            f"no AnkiConnect on {URL} ({e.reason}).\n"
            "  1. Is Anki running?\n"
            "  2. Addon installed? Tools > Add-ons > Get Add-ons > 2055492159, then restart.\n"
            "  3. Did AnkiConnect say 'Failed to listen on port'? The port is probably in a\n"
            "     Windows reserved range, not genuinely in use. Check with:\n"
            "         netsh interface ipv4 show excludedportrange protocol=tcp\n"
            "     Pick a port outside every listed range, set it in the addon config\n"
            "     (Tools > Add-ons > AnkiConnect > Config, \"webBindPort\"), restart Anki,\n"
            "     then point this tool at it:  set ANKI_CONNECT_URL=http://127.0.0.1:<port>"
        )
    if body.get("error"):
        sys.exit(f"AnkiConnect error on '{action}': {body['error']}")
    return body["result"]


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


def expected_deck(notetype, tags):
    for prefix, owns in OWNERSHIP:
        if owns(notetype, tags):
            return prefix
    return None


# --- commands ---------------------------------------------------------------

def cmd_ping(_):
    print(f"AnkiConnect v{call('version')}")
    print(f"decks: {len(call('deckNames'))}  notes: {len(call('findNotes', query='deck:*'))}")


def cmd_decks(_):
    stats = call("getDeckStats", decks=call("deckNames"))
    rows = sorted(stats.values(), key=lambda d: d["name"])
    print(f"{'deck':<32} {'total':>7} {'new':>6} {'learn':>6} {'due':>6}")
    for d in rows:
        print(f"{d['name']:<32} {d['total_in_deck']:>7} {d['new_count']:>6} "
              f"{d['learn_count']:>6} {d['review_count']:>6}")


def cmd_audit(_):
    bad = []
    for note in notes_for("deck:*"):
        want = expected_deck(note["modelName"], note["tags"])
        if want is None:
            continue
        for deck in note["decks"]:
            if not deck.startswith(want):
                bad.append((deck, want, note))
                break
    if not bad:
        print("clean: every note sits under the deck its notetype/tags claim.")
        return
    print(f"{len(bad)} misfiled note(s):\n")
    for deck, want, note in bad:
        print(f"  in '{deck}' -> belongs under '{want}'  [{note['modelName']}] "
              f"tags={' '.join(note['tags'])}\n    {first_field(note)[:110]}")
    print("\nfix e.g.:  ankictl.py move \"deck:Mandarin tag:sec-plus::*\" "
          "--to \"Sec+::SY0-701\" --apply")


def cmd_find(args):
    notes = notes_for(args.query)
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
    current = call("getDeckConfig", deck=args.deck)
    print(f"'{args.deck}' is on preset '{current['name']}' -> clone as '{args.clone}'")
    if not args.apply:
        print("DRY RUN. re-run with --apply.")
        return
    current["name"] = args.clone
    new_id = call("cloneDeckConfigId", name=args.clone, cloneFrom=current["id"])
    if new_id is False:
        sys.exit("clone failed")
    call("setDeckConfigId", decks=[args.deck], configId=new_id)
    print(f"'{args.deck}' now on its own preset '{args.clone}' (id {new_id}).")


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


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping").set_defaults(fn=cmd_ping)
    sub.add_parser("decks").set_defaults(fn=cmd_decks)
    sub.add_parser("audit").set_defaults(fn=cmd_audit)

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
    pr.add_argument("--deck", required=True)
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

    args = p.parse_args()
    try:
        args.fn(args)
    except AnkiDown as e:
        sys.exit(str(e))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
