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


def bypass_conditions(node, node_type, polarity, is_target, not_in, in_any):
    """Rewrite a condition tree so the conditions picked by is_target can be bypassed.

    not_in: conditions (all true) meaning "not bypassed"; in_any: conditions (any true)
    meaning "bypassed".
    polarity "hide": a target condition C becomes (C and not bypassed), i.e. false when bypassed.
    polarity "show": it becomes (C or bypassed), i.e. true when bypassed.
    """
    for g in node.get("conditionGroups", []):
        bypass_conditions(g, g.get("type", "and"), polarity, is_target, not_in, in_any)
    local = [c for c in node.get("conditions", []) if is_target(c)]
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


def relax_conditions(node, node_type, polarity, slot_ids):
    """Rewrite a condition tree so unit-level conditions only count outside a slot."""
    not_in = [{"childId": s, "field": "selections", "scope": "ancestor", "shared": True,
               "type": "notInstanceOf", "value": 1} for s in slot_ids]
    in_any = [{"childId": s, "field": "selections", "scope": "ancestor", "shared": True,
               "type": "instanceOf", "value": 1} for s in slot_ids]
    bypass_conditions(node, node_type, polarity,
                      lambda c: c.get("scope") not in ARMY_SCOPES and c.get("childId") not in slot_ids,
                      not_in, in_any)


def polarity(modifier):
    return "hide" if modifier.get("value") in (True, "true") else "show"


def hidden_toggles(element):
    """(condition holder, polarity) for every conditional hidden toggle on an element."""
    def conditional(m):
        return m.get("conditions") or m.get("conditionGroups")

    found = []
    for m in element.get("modifiers", []):
        if m.get("field") == "hidden" and conditional(m):
            found.append((m, polarity(m)))
    for mg in element.get("modifierGroups", []):
        hidden = [m for m in mg.get("modifiers", []) if m.get("field") == "hidden"]
        if not hidden:
            continue
        kinds = {polarity(m) for m in hidden}
        if conditional(mg):
            if len(kinds) > 1:
                print(f"  WARNING: mixed hide/show modifier group on {element.get('name')} left as is")
            else:
                found.append((mg, kinds.pop()))
        found += [(m, polarity(m)) for m in hidden if conditional(m)]
    return found


def all_conditions(node):
    yield from node.get("conditions", [])
    for g in node.get("conditionGroups", []):
        yield from all_conditions(g)


def relax_hidden_modifiers(element, slot_ids):
    """Apply relax_conditions to every conditional hidden toggle on an enhancement or its groups."""
    for holder, pol in hidden_toggles(element):
        relax_conditions(holder, "and", pol, slot_ids)


def add_overdrive_choices(cat, commanders, sig, od):
    """'Overdrive: <weapon>' choices under the signature system, one per ranged weapon a
    Commander can carry, each shown only while that weapon is equipped. Choosing one adds
    the OVERDRIVE keyword to that weapon, via a modifier on the weapon entry itself."""
    shared = {e["id"]: e for e in cat.get("sharedSelectionEntries", [])}

    def ranged_weapons(node, acc):
        for e in node.get("selectionEntries", []):
            weapon(e, acc)
        for l in node.get("entryLinks", []):
            if l.get("type") == "selectionEntry" and l["targetId"] in shared:
                weapon(shared[l["targetId"]], acc)
        for g in node.get("selectionEntryGroups", []):
            ranged_weapons(g, acc)
        return acc

    def weapon(e, acc):
        if any(p.get("typeId") == od["rangedProfileTypeId"] for p in e.get("profiles", [])):
            acc.setdefault(e["id"], e)
        elif e.get("type") != "model":   # another model's weapons (e.g. drones) aren't the bearer's
            ranged_weapons(e, acc)

    weapons = {}
    for c in commanders:
        found = ranged_weapons(c, {})
        if not found:
            fail(f"no ranged weapons found on '{c['name']}' - upstream structure changed")
        weapons.update(found)

    # Each choice covers every copy of its weapon, so it costs one hidden "Overdrive Weapons"
    # per copy carried and the signature system caps that tally (the same shape as the Orks'
    # per-unit Enhancements cap). Counting selections instead would need a min = max
    # constraint, which New Recruit treats as a fixed entry and removes from the options.
    cost = od["costType"]
    choices = []
    for wid, w in sorted(weapons.items(), key=lambda kv: kv[1]["name"]):
        oid = derived_id("overdrive", wid)
        carried = {"childId": wid, "field": "selections", "includeChildSelections": True,
                   "scope": "root-entry", "shared": True}
        choices.append({
            "type": "upgrade", "import": True, "name": f"{od['keyword']}: {w['name']}", "id": oid, "hidden": False,
            "constraints": [{"id": derived_id(oid, "max"), "field": "selections", "scope": "parent",
                             "shared": True, "type": "max", "value": 1}],
            "costs": [{"name": "pts", "typeId": "51b2-306e-1021-d207", "value": 0},
                      {"name": cost["name"], "typeId": cost["id"], "value": 0}],
            "modifiers": [
                {"field": "hidden", "type": "set", "value": True,
                 "conditions": [dict(carried, type="lessThan", value=1)]},
                {"comment": "One Overdrive Weapon per copy of this weapon carried",
                 "field": cost["id"], "type": "increment", "value": 1,
                 "repeats": [dict(carried, repeats=1, roundUp=False, value=1)]},
            ],
        })
        w["modifiers"] = [m for m in w.get("modifiers", []) if m.get("value") != od["keyword"]] + [{
            "affects": "profiles.Ranged Weapons", "field": od["keywordsCharacteristicId"], "join": ", ",
            "type": "append", "value": od["keyword"],
            "conditions": [{"childId": oid, "field": "selections", "includeChildSelections": True,
                            "scope": "root-entry", "shared": True, "type": "atLeast", "value": 1}],
        }]
    sig["selectionEntryGroups"] = [{
        "name": "Overdrive Weapons", "id": "c4a1-5eed-0000-0024", "hidden": False, "collapsible": False,
        "selectionEntries": choices,
    }]
    sig["constraints"] = replace_by_id(sig.get("constraints", []), [{
        "id": "c4a1-5eed-0000-0025", "field": cost["id"], "includeChildSelections": True, "scope": "self",
        "shared": True, "type": "max", "value": od["maxWeapons"],
        "message": f"Overdrive covers at most {od['maxWeapons']} weapons, and every copy of a chosen weapon counts.",
    }])
    print("Overdrive choices: " + ", ".join(w["name"] for w in sorted(weapons.values(), key=lambda w: w["name"])))


def apply_enhancement_unlocks(cats, gst, groups, entries, is_enhancement, cat_groups, cp):
    """Enhancement Unlocks: under Campaign Purchases the player ticks specific enhancements from
    their faction. Each ticked one stays available when its own detachment isn't taken.

    Enhancements are hidden by "detachment not taken" checks on the enhancement or on its
    detachment's group. Each such check is bypassed while a matching unlock is ticked. A group
    opened that way would show its other enhancements too, so each of those gets its own copy
    of the group's check, bypassed only by its own unlock."""
    det = {}   # detachment entry id -> name
    for doc in list(cats.values()) + [gst]:
        for n in walk_nodes(doc):
            for g in n.get("sharedSelectionEntryGroups", []) + n.get("selectionEntryGroups", []):
                if g.get("name") in cp["detachmentGroupNames"]:
                    det.update({e["id"]: e["name"] for e in g.get("selectionEntries", [])})
                    det.update({l["targetId"]: l["name"] for l in g.get("entryLinks", [])})
    if not det:
        fail("no detachments found - upstream structure changed")

    def absent(c):    # "this detachment is not taken"
        t, v = c.get("type"), c.get("value", 0)
        return c.get("childId") in det and (t == "notInstanceOf" or (t == "lessThan" and v <= 1)
                                            or (t == "equalTo" and v == 0))

    def present(c):   # "this detachment is taken"
        t, v = c.get("type"), c.get("value", 0)
        return c.get("childId") in det and (t in ("instanceOf", "atLeast", "greaterThan")
                                            or (t == "equalTo" and v >= 1))

    target = {"hide": absent, "show": present}

    def gates(el):
        """(holder, polarity) toggles on el that depend on a detachment being taken."""
        return [(h, p) for h, p in hidden_toggles(el) if any(target[p](c) for c in all_conditions(h))]

    def det_ids(el):
        return [c["childId"] for h, p in gates(el) for c in all_conditions(h) if target[p](c)]

    info = {}        # enhancement id -> {"el", "dets", "own", "cats"}
    gated = {}       # group id -> (group, enhancement ids under it)

    def record(e, gate_chain):
        own = det_ids(e)
        dets = own + [d for _, ds in gate_chain for d in ds]
        if not dets:
            return   # available in every detachment already
        i = info.setdefault(e["id"], {"el": e, "dets": [], "own": bool(own), "cats": set()})
        i["dets"] += [d for d in dets if d not in i["dets"]]
        for g, _ in gate_chain:
            gated.setdefault(g["id"], (g, set()))[1].add(e["id"])

    def visit(gid, gate_chain, seen):
        if gid in seen or gid not in groups:
            return
        seen = seen | {gid}
        g = groups[gid]
        ds = det_ids(g)
        chain = gate_chain + ([(g, ds)] if ds else [])
        for e in g.get("selectionEntries", []):
            if is_enhancement(e):
                record(e, chain)
        for sub in g.get("selectionEntryGroups", []):
            visit(sub["id"], chain, seen)
        for l in g.get("entryLinks", []):
            if l.get("type") == "selectionEntryGroup":
                visit(l["targetId"], chain, seen)
            elif is_enhancement(entries.get(l["targetId"], {})):
                record(entries[l["targetId"]], chain)

    # Every linked group, not just the characters' Enhancements: some detachments put their
    # enhancements on other units (T'au Advanced Acquisition Cadre on Stealth Battlesuits).
    linked = {l["targetId"] for doc in list(cats.values()) + [gst] for n in walk_nodes(doc)
              for l in n.get("entryLinks", []) if l.get("type") == "selectionEntryGroup"}
    for gid in sorted(linked | set().union(*cat_groups.values())):
        visit(gid, [], frozenset())
    if not info:
        fail("no detachment-gated enhancements found - upstream structure changed")

    # An army can unlock an enhancement when the enhancement's detachment is one of the army's
    # own Detachment choices. Those come from its own root entries. An army without its own
    # Detachment entry (a chapter, Chaos Daemons) borrows the one from the catalogue it imports
    # that offers the most detachments, so allies' detachments (Agents of the Imperium) stay out.
    by_id = {c["id"]: c for c in cats.values()}

    def detachments_of(cat):
        found, seen, stack = set(), set(), [l["targetId"] for l in cat.get("entryLinks", [])]
        while stack:
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            node = entries.get(nid) or groups.get(nid)
            if node is None:
                continue
            if node.get("name") in cp["detachmentGroupNames"] and nid in groups:
                found |= {e["id"] for e in node.get("selectionEntries", [])}
                found |= {l["targetId"] for l in node.get("entryLinks", [])}
            stack += [s["id"] for s in node.get("selectionEntries", []) + node.get("selectionEntryGroups", [])]
            stack += [l["targetId"] for l in node.get("entryLinks", [])]
        return found

    names = {c["id"]: n[:-5] for n, c in cats.items()}
    borrowed = []
    for c in cats.values():
        if c.get("library"):
            continue
        reach = detachments_of(c)
        if not reach:
            offers = [(detachments_of(by_id[l["targetId"]]), l["targetId"]) for l in c.get("catalogueLinks", [])
                      if l.get("importRootEntries") and l["targetId"] in by_id]
            reach, src = max(offers, key=lambda o: len(o[0]), default=(set(), None))
            if reach:
                borrowed.append(f"{names[c['id']]} <- {names[src]}")
        for i in info.values():
            if reach & set(i["dets"]):
                i["cats"].add(c["id"])
    orphans = [eid for eid, i in info.items() if not i["cats"]]
    for eid in orphans:
        del info[eid]
    for g, eids in gated.values():
        eids -= set(orphans)

    uid = {eid: derived_id("unlock", eid) for eid in info}

    def not_unlocked(eids):
        return [{"childId": uid[e], "field": "selections", "includeChildForces": True, "includeChildSelections": True,
                 "scope": "roster", "shared": True, "type": "lessThan", "value": 1} for e in sorted(eids)]

    def unlocked(eids):
        return [dict(c, type="atLeast") for c in not_unlocked(eids)]

    def open_for(el, eids):
        for h, p in gates(el):
            bypass_conditions(h, "and", p, target[p], not_unlocked(eids), unlocked(eids))

    # Groups first: keep a copy of each "hide unless detachment" check for the enhancements inside.
    n_siblings = 0
    for g, eids in gated.values():
        hide_checks = [copy.deepcopy({k: h[k] for k in ("conditions", "conditionGroups") if k in h})
                       for h, p in gates(g) if p == "hide"]
        if len(hide_checks) < len(gates(g)):
            print(f"  WARNING: '{g.get('name')}' shows by detachment; its other enhancements may show with an unlock")
        open_for(g, eids)
        for eid in eids:
            for check in hide_checks:
                m = dict(copy.deepcopy(check), field="hidden", type="set", value=True,
                         comment=f"Only an unlocked enhancement shows outside its detachment ({g.get('name')})")
                bypass_conditions(m, "and", "hide", absent, not_unlocked([eid]), unlocked([eid]))
                info[eid]["el"].setdefault("modifiers", []).append(m)
                n_siblings += 1
    for eid, i in info.items():
        if i["own"]:
            open_for(i["el"], [eid])

    # The choices under Campaign Purchases, one per unlockable enhancement, shown only to its armies.
    cost = cp["unlockCostType"]
    gst["costTypes"] = replace_by_id(gst.get("costTypes", []), [cost])
    choices = []
    for eid, i in sorted(info.items(), key=lambda kv: (det[kv[1]["dets"][0]], kv[1]["el"]["name"])):
        u = uid[eid]
        choices.append({
            "type": "upgrade", "import": True, "name": f"{det[i['dets'][0]]}: {i['el']['name']}", "id": u,
            "hidden": False,
            "constraints": [{"id": derived_id(u, "max"), "field": "selections", "scope": "parent",
                             "shared": True, "type": "max", "value": 1}],
            "costs": [{"name": "pts", "typeId": "51b2-306e-1021-d207", "value": 0},
                      {"name": cost["name"], "typeId": cost["id"], "value": 1}],
            "modifiers": [{"comment": "Only for armies that can field this enhancement",
                           "field": "hidden", "type": "set", "value": True,
                           "conditions": [{"childId": c, "field": "selections", "scope": "primary-catalogue",
                                           "shared": True, "type": "notInstanceOf", "value": 1}
                                          for c in sorted(i["cats"])]}],
        })
    unlock = next((e for e in cp["purchases"]["selectionEntries"] if e["id"] == cp["unlockEntryId"]), None)
    if unlock is None:
        fail(f"Enhancement Unlock purchase {cp['unlockEntryId']} missing from campaign.json")
    unlock["selectionEntries"] = choices

    per_army = {}
    for i in info.values():
        for c in i["cats"]:
            per_army[c] = per_army.get(c, 0) + 1
    print(f"Enhancement unlocks: {len(choices)} enhancements ({len(gated)} detachment groups opened, "
          f"{n_siblings} sibling checks added, {len(orphans)} with no army's detachment skipped)")
    print("  per army: " + ", ".join(f"{names[c]} {n}" for c, n in sorted(per_army.items(), key=lambda kv: names[kv[0]])))
    print("  detachments borrowed: " + ", ".join(borrowed))


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
    cat_groups = {}           # catalogue id -> enhancement groups its characters use
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
            cat_groups.setdefault(cat["id"], set()).update(gids)
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

    apply_enhancement_unlocks(cats, gst, groups, entries, is_enhancement, cat_groups, cp)

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
    gst["gameSystem"]["costTypes"] = replace_by_id(gst["gameSystem"].get("costTypes", []),
                                                   [cfg["overdrive"]["costType"]])
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

    # 3d. Signature system: a 0 pt Bork'an enhancement in the T'au Enhancements list, so it uses
    #     the bearer's Enhancement (or a Campaign Enhancement Slot) and counts towards the army
    #     limit. Commander models only: there's no shared COMMANDER keyword in the data, so it
    #     checks each Commander's own name category.
    root_targets = {l["targetId"] for l in cat.get("entryLinks", [])}
    pattern = re.compile(cfg["commanderNamePattern"])
    commanders = [e for e in cat["sharedSelectionEntries"]
                  if e["id"] in root_targets and pattern.search(e.get("name", ""))]
    commander_cats = [c for c in cat.get("categoryEntries", []) if pattern.search(c.get("name", ""))]
    if not commanders or not commander_cats:
        fail(f"no Commander units/categories matched {cfg['commanderNamePattern']!r}")
    sig.setdefault("modifiers", []).append({
        "comment": "Commander models only",
        "field": "hidden", "type": "set", "value": True,
        "conditions": [{"childId": c["id"], "field": "selections", "scope": "ancestor", "shared": True,
                        "type": "notInstanceOf", "value": 1} for c in commander_cats],
    })
    enh_group = next((g for g in cat.get("sharedSelectionEntryGroups", [])
                      if g["id"] == cfg["enhancementGroupId"]), None)
    if enh_group is None:
        fail(f"Enhancements group {cfg['enhancementGroupId']} not found - upstream structure changed")
    enh_group["selectionEntryGroups"] = replace_by_id(enh_group.get("selectionEntryGroups", []),
                                                      [cfg["borkanEnhancements"]])
    print(f"Signature system added to '{enh_group['name']}' for: " + ", ".join(c["name"] for c in commander_cats))
    add_overdrive_choices(cat, commanders, sig, cfg["overdrive"])

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
