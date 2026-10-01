#!/usr/bin/env python3
"""Rebuild the repo root from a fresh upstream checkout, then layer the campaign
additions from overlay/campaign.json on top.

    python3 overlay/apply.py --upstream .upstream --out .

Exits non-zero if the upstream structure has changed so that a patch point can
no longer be found, so a broken overlay never ships silently.
"""
import argparse
import copy
import hashlib
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


def derived_id(*parts):
    """Stable id for generated entries, so saved rosters survive re-syncs."""
    h = hashlib.sha1("|".join(parts).encode()).hexdigest()
    return f"c4a1-{h[:4]}-{h[4:8]}-{h[8:12]}"


def walk_nodes(obj):
    """Every dict in a BattleScribe tree, depth first."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_nodes(v)


# Scopes that look at the army rather than at the unit an enhancement sits on.
# Conditions using them (detachment chosen, Crusade force, already taken elsewhere)
# still apply inside a campaign slot; everything else (keywords, Epic Hero,
# per-unit checks) is switched off there.
ARMY_SCOPES = {"roster", "force"}


def relax_conditions(node, node_type, polarity, slot_ids):
    """Rewrite a condition tree so unit-level conditions only count outside a slot.

    polarity "hide": such a condition becomes (C and not in a slot), i.e. false in a slot.
    polarity "show": it becomes (C or in a slot), i.e. true in a slot.
    """
    not_in = [{"childId": s, "field": "selections", "scope": "ancestor", "shared": True,
               "type": "notInstanceOf", "value": 1} for s in slot_ids]
    in_any = [{"childId": s, "field": "selections", "scope": "ancestor", "shared": True,
               "type": "instanceOf", "value": 1} for s in slot_ids]
    for g in node.get("conditionGroups", []):
        relax_conditions(g, g.get("type", "and"), polarity, slot_ids)
    local = [c for c in node.get("conditions", []) if c.get("scope") not in ARMY_SCOPES
             and c.get("childId") not in slot_ids]
    if not local:
        return
    if polarity == "hide" and node_type == "and":
        node["conditions"] += [c for c in not_in if c not in node["conditions"]]
        return
    if polarity == "show" and node_type == "or":
        node["conditions"] += [c for c in in_any if c not in node["conditions"]]
        return
    # Mixed case: wrap each unit-level condition in its own group.
    node["conditions"] = [c for c in node["conditions"] if c not in local]
    node.setdefault("conditionGroups", []).extend(
        {"type": "and", "conditions": [c] + not_in} if polarity == "hide"
        else {"type": "or", "conditions": [c] + in_any}
        for c in local)


def polarity(modifier):
    return "hide" if modifier.get("value") in (True, "true") else "show"


def relax_hidden_modifiers(element, slot_ids):
    """Apply relax_conditions to every conditional hidden toggle on an enhancement or its groups."""
    def conditional(m):
        return m.get("conditions") or m.get("conditionGroups")

    for m in element.get("modifiers", []):
        if m.get("field") == "hidden" and conditional(m):
            relax_conditions(m, "and", polarity(m), slot_ids)
    for mg in element.get("modifierGroups", []):
        hidden = [m for m in mg.get("modifiers", []) if m.get("field") == "hidden"]
        if not hidden:
            continue
        kinds = {polarity(m) for m in hidden}
        if conditional(mg):
            if len(kinds) > 1:
                print(f"  WARNING: mixed hide/show modifier group on {element.get('name')} left as is")
            else:
                relax_conditions(mg, "and", kinds.pop(), slot_ids)
        for m in hidden:
            if conditional(m):
                relax_conditions(m, "and", polarity(m), slot_ids)


def apply_campaign_purchases(out, gst_path, cp):
    """Hardened Wargear and the Bonus Enhancement Slot, on every faction's characters."""
    char_cat, enh_cost = cp["characterCategoryId"], cp["enhancementCostTypeId"]

    # Game system: cost type, the root "Campaign Purchases" entry, and the two Hardened Wargear options.
    gst_root = read_json(gst_path)
    gst = gst_root["gameSystem"]
    gst["costTypes"] = replace_by_id(gst.get("costTypes", []), [cp["slotCostType"]])
    hardened = [cp["hardenedToughness"], cp["hardenedSave"]]
    gst["sharedSelectionEntries"] = replace_by_id(gst.get("sharedSelectionEntries", []),
                                                  [cp["purchases"]] + hardened)
    add_entry_link(gst, cp["purchases"], cp["purchasesRootLinkId"])

    files = {f.name: read_json(f) for f in sorted(out.iterdir())
             if f.is_file() and f.suffix == ".json" and f != gst_path}
    cats = {name: r["catalogue"] for name, r in files.items() if "catalogue" in r}

    # Global indexes: every shared group/entry by id, and every unit linked at a catalogue root.
    groups, entries = {}, {}
    for doc in list(cats.values()) + [gst]:
        for n in walk_nodes(doc):
            for g in n.get("sharedSelectionEntryGroups", []) + n.get("selectionEntryGroups", []):
                groups.setdefault(g["id"], g)
            for e in n.get("sharedSelectionEntries", []) + n.get("selectionEntries", []):
                entries.setdefault(e["id"], e)
    root_targets = {l["targetId"] for doc in list(cats.values()) + [gst] for l in doc.get("entryLinks", [])}

    def is_enhancement(e):
        return e.get("type") == "upgrade" and any(
            c.get("typeId") == enh_cost and c.get("value") == 1 for c in e.get("costs", []))

    def group_elements(gid, seen=None):
        """(groups, enhancements) reachable from a group, following links."""
        seen = set() if seen is None else seen
        if gid in seen or gid not in groups:
            return [], []
        seen.add(gid)
        g = groups[gid]
        gs, es = [g], [e for e in g.get("selectionEntries", []) if is_enhancement(e)]
        for sub in g.get("selectionEntryGroups", []):
            sg, se = group_elements(sub["id"], seen)
            gs += sg; es += se
        for l in g.get("entryLinks", []):
            if l.get("type") == "selectionEntryGroup":
                sg, se = group_elements(l["targetId"], seen)
                gs += sg; es += se
            elif is_enhancement(entries.get(l["targetId"], {})):
                es.append(entries[l["targetId"]])
        return gs, es

    def enhancement_groups(unit):
        return [l["targetId"] for l in unit.get("entryLinks", [])
                if l.get("type") == "selectionEntryGroup" and group_elements(l["targetId"])[1]]

    def max_toughness(unit):
        ts = [int(ch["$text"]) for n in walk_nodes(unit) if n.get("typeName") == "Unit"
              for ch in n.get("characteristics", []) if ch.get("typeId") == cp["toughnessCharacteristicId"]
              and str(ch.get("$text", "")).isdigit()]
        return max(ts, default=0)

    characters = {name: [e for e in cat.get("sharedSelectionEntries", []) if e["id"] in root_targets
                         and any(l.get("targetId") == char_cat for l in e.get("categoryLinks", []))]
                  for name, cat in cats.items()}
    if not any(characters.values()):
        fail("no CHARACTER units found - upstream structure changed")

    # Characters without an Enhancements link (Epic Heroes) get their catalogue's usual group,
    # or failing that the usual group of the first non-library catalogue it imports that has one
    # (a chapter's Space Marines). Libraries are skipped so Agents don't inherit Knight enhancements.
    usual = {}
    for name, chars in characters.items():
        counts = {}
        for c in chars:
            for gid in enhancement_groups(c):
                counts[gid] = counts.get(gid, 0) + 1
        if counts:
            usual[cats[name]["id"]] = max(counts, key=counts.get)
    libraries = {cat["id"] for cat in cats.values() if cat.get("library")}

    slot_tpl = cp["slot"]
    slots_by_group = {}       # group id -> slot ids that link it
    n_hardened = n_slotted = 0
    unslotted = []
    for name, chars in characters.items():
        cat = cats[name]
        candidates = [cat["id"]] + [l["targetId"] for l in cat.get("catalogueLinks", [])
                                    if l["targetId"] not in libraries]
        fallback = [usual[c] for c in candidates if c in usual][:1]
        new_slots = {}
        for c in chars:
            if max_toughness(c) <= cp["maxToughness"]:
                for h in hardened:
                    add_entry_link(c, h, derived_id(c["id"], h["id"]))
                n_hardened += 1
            gids = enhancement_groups(c) or fallback
            if not gids:
                unslotted.append(c["name"])
                continue
            for gid in gids:
                sid = derived_id(name, gid, "slot")
                if sid not in new_slots:
                    slot = copy.deepcopy(slot_tpl)
                    slot.pop("_comment", None)
                    slot["id"] = sid
                    for i, k in enumerate(slot["constraints"]):
                        k["id"] = derived_id(sid, "constraint", str(i))
                    for r in slot.get("rules", []):
                        r["id"] = derived_id(sid, "rule")
                    slot["entryLinks"] = [{
                        "name": groups[gid]["name"], "id": derived_id(sid, gid), "hidden": False,
                        "import": True, "targetId": gid, "type": "selectionEntryGroup",
                        "constraints": [{"id": derived_id(sid, gid, "min"), "field": "selections",
                                         "includeChildSelections": True, "scope": "self", "shared": True,
                                         "type": "min", "value": 1}],
                    }]
                    new_slots[sid] = slot
                    slots_by_group.setdefault(gid, set()).add(sid)
                add_entry_link(c, new_slots[sid], derived_id(c["id"], sid))
            n_slotted += 1
        if new_slots:
            cat["sharedSelectionEntries"] = replace_by_id(cat.get("sharedSelectionEntries", []),
                                                          list(new_slots.values()))
    if not slots_by_group:
        fail("no Enhancements groups found on CHARACTER units - upstream structure changed")

    # Inside a slot, an enhancement ignores unit-level restrictions but keeps army-level ones.
    reach = {}
    for gid, sids in slots_by_group.items():
        gs, es = group_elements(gid)
        for el in gs + es:
            reach.setdefault(id(el), (el, set()))[1].update(sids)
    for el, sids in reach.values():
        relax_hidden_modifiers(el, sorted(sids))

    # A slotted enhancement sits one level deeper (unit > slot > enhancement), so unit-level
    # checks for it elsewhere must look into child selections to find it.
    slotted = {el["id"] for el, _ in reach.values() if is_enhancement(el)}
    deepened = 0
    for doc in list(cats.values()) + [gst]:
        for n in walk_nodes(doc):
            if (n.get("childId") in slotted and n.get("scope") not in ARMY_SCOPES
                    and not n.get("includeChildSelections")):
                n["includeChildSelections"] = True
                deepened += 1

    for name, root in files.items():
        write_json(root, out / name)
    write_json(gst_root, gst_path)
    print(f"Campaign purchases: Hardened Wargear on {n_hardened} characters, enhancement slot on "
          f"{n_slotted} ({len(slots_by_group)} enhancement groups, {len(reach)} groups/enhancements relaxed, {deepened} unit-level checks deepened)")
    if unslotted:
        print(f"  No enhancement group found for {len(unslotted)} characters: {', '.join(sorted(unslotted))}")


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

    # 3a. The Bork'an detachment (0 DP) that every campaign rule below is gated on.
    det_group = next((g for g in cat.get("sharedSelectionEntryGroups", [])
                      if g["id"] == cfg["detachmentGroupId"]), None)
    if det_group is None:
        fail(f"Detachment group {cfg['detachmentGroupId']} not found - upstream structure changed")
    det_group["selectionEntries"] = replace_by_id(det_group.get("selectionEntries", []), [cfg["detachment"]])
    print(f"Detachment '{cfg['detachment']['name']}' added to '{det_group['name']}'")

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
    root_targets = {l["targetId"] for l in cat.get("entryLinks", [])}
    pattern = re.compile(cfg["commanderNamePattern"])
    commanders = [e for e in cat["sharedSelectionEntries"]
                  if e["id"] in root_targets and pattern.search(e.get("name", ""))]
    if not commanders:
        fail(f"no Commander units matched {cfg['commanderNamePattern']!r}")
    for i, c in enumerate(commanders):
        add_entry_link(c, sig, f"c4a1-5eed-2{i:03x}-0020")
    print("Signature system linked on: " + ", ".join(c["name"] for c in commanders))

    # 3e. Experimental Prototype Cadre weapon upgrades on Farsight's rifle (campaign ruling).
    xp = cfg["experimentalPrototype"]
    by_id = {n["id"]: n for n in walk_nodes(cat) if "id" in n and "targetId" not in n}
    for wid in xp["plasmaRifleWeapons"]:
        weapon = by_id.get(wid) or fail(f"weapon {wid} not found - upstream structure changed")
        weapon["categoryLinks"] = replace_by_id(weapon.get("categoryLinks", []), [xp["plasmaRifleCategory"]])
        weapon["entryLinks"] = replace_by_id(weapon.get("entryLinks", []), [xp["weaponUpgradesLink"]])
        print(f"Plasma rifle weapon upgrades enabled on '{weapon['name']}' ({wid})")
    for uid in xp["weaponUpgradeIds"]:
        upgrade = by_id.get(uid) or fail(f"weapon upgrade {uid} not found - upstream structure changed")
        dropped = 0
        for n in walk_nodes(upgrade.get("modifiers", [])):
            if "conditions" not in n:
                continue
            before = len(n["conditions"])
            n["conditions"] = [c for c in n.get("conditions", []) if c.get("childId") != xp["epicHeroCategoryId"]]
            dropped += before - len(n["conditions"])
        if not dropped:
            fail(f"no Epic Hero check found on '{upgrade['name']}' - upstream structure changed")
        print(f"Epic Hero check dropped from '{upgrade['name']}'")

    write_json(root, cat_path)

    # 4. Campaign token purchases, for every faction.
    apply_campaign_purchases(args.out, gst_path, cfg["campaignPurchases"])
    print("Overlay applied.")


if __name__ == "__main__":
    main()
