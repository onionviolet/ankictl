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
  find "<anki search>"                  list matching notes
  audit [query]                         find cards sitting in the wrong deck
  fields --notetype "<name>"            list fields
  stats                                 collection shape, for orientation

Writing (DRY RUN unless --apply)
  add --file notes.json                 create notes; '-' reads stdin
  update --file edits.json              rewrite fields of existing notes
  move "<anki search>" --to "<deck>"    change deck, scheduling preserved
  suspend / unsuspend "<anki search>"   take cards out of / back into rotation
  limits --deck D [--new N --rev N]     daily limits
  preset --deck D [D2...] --clone NAME  give decks their own options preset
  addfield --notetype N --field F       append a field to a note type

Every command takes --json for machine-readable output, which is what an agent
should use. Anki search syntax is the same as the Browse bar: deck:Spanish,
tag:chem::*, note:Cloze, -deck:Spanish::*, "deck:Spanish -note:Basic".

Port: defaults to 127.0.0.1:8765. Override with the ANKI_CONNECT_URL env var when
that port is unavailable (see the reserved-range note in the README).

See AGENTS.md for the usage contract an AI should follow.
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
    if blocked and not args.json:
        print(f"{len(blocked)} note(s) blocked as duplicate/empty: {blocked}")
    ids = call("addNotes", notes=notes)
    added = [i for i in ids if i]
    if args.json:
        return emit({"added": len(added), "noteIds": ids})
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
