#!/usr/bin/env python3
"""Rebuild the repo root from a fresh upstream checkout, then layer the campaign
additions from overlay/campaign.json on top.

    python3 overlay/apply.py --upstream .upstream --out .

Exits non-zero if the upstream structure has changed so that a patch point can
no longer be found, so a broken overlay never ships silently.
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

DATA_EXT = {".json", ".gst", ".cat", ".gstz", ".catz"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(obj, path):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def add_entry_link(host, target, link_id):
    links = [l for l in host.get("entryLinks", []) if l.get("targetId") != target["id"]]
    links.append({
        "name": target["name"], "id": link_id, "hidden": False, "import": True,
        "targetId": target["id"], "type": "selectionEntry",
    })
    host["entryLinks"] = links


def replace_by_id(items, new_items):
    ids = {i["id"] for i in new_items}
    return [i for i in items if i.get("id") not in ids] + new_items


def fail(msg):
    sys.exit(f"ERROR: {msg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    cfg = read_json(Path(__file__).with_name("campaign.json"))

    # 1. Mirror upstream data files into the output root (and drop ones upstream deleted).
    up_files = [f for f in args.upstream.iterdir() if f.is_file() and f.suffix in DATA_EXT]
    if not up_files:
        fail(f"no data files found in {args.upstream}")
    up_names = {f.name for f in up_files}
    for f in args.out.iterdir():
        if f.is_file() and f.suffix in DATA_EXT and f.name not in up_names:
            print(f"Removing {f.name} (gone upstream)")
            f.unlink()
    for f in up_files:
        shutil.copyfile(f, args.out / f.name)

    # 2. Tag the game system name so it's distinguishable from the official one in New Recruit.
    gst_path = args.out / cfg["gameSystemFile"]
    gst = read_json(gst_path)
    suffix = cfg.get("gameSystemNameSuffix")
    if suffix and not gst["gameSystem"]["name"].endswith(suffix):
        gst["gameSystem"]["name"] += suffix
    write_json(gst, gst_path)

    # 3. Patch the faction catalogue.
    cat_path = args.out / cfg["catalogueFile"]
    root = read_json(cat_path)
    cat = root.get("catalogue") or fail(f"{cfg['catalogueFile']} has no 'catalogue' root")

    # 3a. Army-wide rules (shown on the force, like For The Greater Good).
    cat["rules"] = replace_by_id(cat.get("rules", []), cfg["armyRules"])

    # 3a'. Sept Tenet stat changes: one modifier group on every root unit datasheet.
    root_targets = {l["targetId"] for l in cat.get("entryLinks", [])}
    units = [e for e in cat.get("sharedSelectionEntries", []) if e["id"] in root_targets]
    if not units:
        fail("no root unit entries found - upstream structure changed")
    group = cfg["unitModifierGroup"]
    for u in units:
        u["modifierGroups"] = [g for g in u.get("modifierGroups", [])
                               if g.get("comment") != group["comment"]] + [group]
    print(f"Sept Tenet profile modifiers added to {len(units)} units")

    # 3b. Shared entries for the trait and signature system.
    trait, sig = cfg["warlordTrait"], cfg["signatureSystem"]
    cat["sharedSelectionEntries"] = replace_by_id(cat.get("sharedSelectionEntries", []), [trait, sig])

    # 3c. Warlord trait: a required child of the Warlord upgrade itself, so ticking
    #     Warlord on any unit auto-selects the trait.
    warlord_id = cfg["warlordEntryId"]
    warlord = next((e for e in cat["sharedSelectionEntries"] if e["id"] == warlord_id), None)
    if warlord is None:
        fail(f"Warlord entry {warlord_id} not found - upstream structure changed")
    add_entry_link(warlord, trait, "c4a1-5eed-1000-0010")
    print(f"Warlord trait attached to '{warlord['name']}' ({warlord_id})")

    # 3d. Signature system: root-level Commander units only.
    pattern = re.compile(cfg["commanderNamePattern"])
    commanders = [e for e in cat["sharedSelectionEntries"]
                  if e["id"] in root_targets and pattern.search(e.get("name", ""))]
    if not commanders:
        fail(f"no Commander units matched {cfg['commanderNamePattern']!r}")
    for i, c in enumerate(commanders):
        add_entry_link(c, sig, f"c4a1-5eed-2{i:03x}-0020")
    print("Signature system linked on: " + ", ".join(c["name"] for c in commanders))

    write_json(root, cat_path)
    print("Overlay applied.")


if __name__ == "__main__":
    main()
