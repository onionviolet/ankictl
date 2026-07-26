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

Anki search syntax is the same as the Browse bar: deck:Spanish, tag:chem::*,
note:Cloze, -deck:Spanish::*, "deck:Spanish -note:Basic".

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

TAG_SEPS = ("::", "/")  # Anki's own hierarchy separator, and the common ad-hoc one


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

    args = p.parse_args()
    try:
        args.fn(args)
    except AnkiDown as e:
        sys.exit(str(e))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
