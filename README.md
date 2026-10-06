# wh40k-11e + T'au campaign overlay

**[Add to New Recruit](https://www.newrecruit.eu/app/MySystems?addSystem=whitestrake%2Fftw-campaign-11e&ref=latest-release-or-commit)**

The data files at the repo root are generated. Every 4 hours a GitHub Action
checks out [BSData/wh40k-11e](https://github.com/BSData/wh40k-11e), copies its
data files here, and applies `overlay/campaign.json` using `overlay/apply.py` (Python 3, standard library only).
**Don't edit the root `.json` files by hand**, because the next sync overwrites them.

## What the overlay adds (all 0 pts)

A **Bork’an** detachment (0 DP) in the T'au Detachment list. Everything else
only applies while it is selected:

| Addition | Where it shows up in New Recruit |
|---|---|
| Sept Tenet: Superior Craftsmanship | Rule on the Bork’an detachment; +4" Range on every ranged weapon (annotated "Sept Tenet") |
| Warlord Trait: Seeker of Perfection | Auto-selected under **Warlord** on whichever unit is your Warlord; AP improved by 1 on its ranged weapons |
| Plasma Accelerator Rifle on Farsight (campaign ruling) | Farsight's High-intensity plasma rifle counts as a plasma rifle for the Experimental Prototype Cadre weapon upgrades, and those upgrades no longer exclude Epic Heroes, so they work from a Campaign Enhancement Slot. This one isn't gated on Bork’an |
| Signature System: Overdrive Power Systems | A 0 pt enhancement under **Bork’an Enhancements** in the Enhancements list, for Commander models. It uses the bearer's Enhancement and counts towards the army's Enhancement limit, so Shadowsun and Farsight can only take it through a Campaign Enhancement Slot. While it's taken, each of the model's ranged weapons gets an **Overdrive** option, up to two in total. Ticking it on a weapon splits that copy off (like the Experimental Prototype Cadre weapon upgrades) and adds **Overdrive** to its keywords, so two of four fusion blasters show separately. The Overdrive rule appears in the model's rules |

## Campaign purchases (every faction)

Token purchases are recorded once per army under **Campaign Purchases** in the
Configuration section. Ticking one makes the matching option appear on that
faction's **CHARACTER** units:

| Purchase | Character option |
|---|---|
| Hardened Wargear (Toughness), 3 tokens | **Hardened Wargear: Toughness** on any CHARACTER with Toughness 10 or less. +1 Toughness. Max 1 per army |
| Hardened Wargear (Save), 3 tokens | **Hardened Wargear: Save** on the same characters. Save improved by 1. Max 1 per army. Can go on the same character as Toughness |
| Enhancement Unlock, 3 tokens, up to 2 | No character option: tick the specific enhancements you unlocked under **Enhancement Unlocks**, grouped by detachment (only your army's own). At most one per detachment, so two unlocks must come from different detachments. Each one becomes available in the normal Enhancements list whatever detachment you take, and is taken as normal: points, the bearer's Enhancement, the army limit and keyword restrictions all still apply. It can also go in a Campaign Enhancement Slot. Its detachment's other enhancements stay hidden |
| Bonus Enhancement Slot, 4 tokens | **Campaign Enhancement Slot** on any CHARACTER, Epic Heroes included. Holds one enhancement from the current detachment, paid for in points as normal. It doesn't count towards the army's Enhancement limit, and it ignores keyword, Epic Hero and one-per-character restrictions. Each enhancement is still limited to one per army |

Hardened Wargear relies on New Recruit applying a Leader's modifiers to the unit
it leads, so the bonus also shows on the attached unit. Agents of the Imperium
and Unaligned characters get no enhancement slot, because those catalogues have
no enhancements of their own.

The game system shows up as **"Warhammer 40,000 11th Edition (Campaign)"**.

## Setup

1. Create a new **public** GitHub repo and push this folder to `main`.
2. Go to Settings → Actions → General → Workflow permissions and select **Read and write**.
3. Go to Actions → "Sync upstream + apply campaign overlay" → **Run workflow**.
4. Use the **Add to New Recruit** link at the top of this README. Or, in New Recruit, go to *Add or Remove games* → *Add from GitHub* and enter this repo.

## Changing the rules

Edit `overlay/campaign.json` and push. The workflow reruns on its own.
To test locally:

```
python3 overlay/apply.py --upstream <path-to-wh40k-11e-checkout> --out <scratch-dir>
```

If upstream changes the structure so a patch point disappears (the Detachment
group or Warlord entry is gone, or no units are named `Commander …`), the workflow **fails instead of
publishing half-patched data**. GitHub emails you, and New Recruit keeps serving the
last good version.
