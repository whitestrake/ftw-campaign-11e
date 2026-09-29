# wh40k-11e + T'au campaign overlay

The data files at the repo root are generated. Every 4 hours a GitHub Action
checks out [BSData/wh40k-11e](https://github.com/BSData/wh40k-11e), copies its
data files here, and applies `overlay/campaign.json` using `overlay/apply.py` (Python 3, standard library only).
**Don't edit the root `.json` files by hand**, because the next sync overwrites them.

## What the overlay adds (all 0 pts, no DP)

| Addition | Where it shows up in New Recruit |
|---|---|
| Sept Tenet: Superior Craftsmanship | Army rule on the force (next to For The Greater Good) |
| Warlord Trait: Seeker of Perfection | Option on any unit once it has **Warlord** selected. Max 1 per roster |
| Signature System: Overdrive Power Systems | Option on `Commander …` units. Max 1 per roster. Separate from the Enhancements slot |

The game system shows up as **"Warhammer 40,000 11th Edition (Campaign)"**.

## Setup

1. Create a new **public** GitHub repo and push this folder to `main`.
2. Go to Settings → Actions → General → Workflow permissions and select **Read and write**.
3. Go to Actions → "Sync upstream + apply campaign overlay" → **Run workflow**.
4. In New Recruit, go to *Add or Remove games* → *Add from GitHub* and enter this repo.

## Changing the rules

Edit `overlay/campaign.json` and push. The workflow reruns on its own.
To test locally:

```
python3 overlay/apply.py --upstream <path-to-wh40k-11e-checkout> --out <scratch-dir>
```

If upstream changes the structure so a patch point disappears (no Warlord
links found, or no units named `Commander …`), the workflow **fails instead of
publishing half-patched data**. GitHub emails you, and New Recruit keeps serving the
last good version.
