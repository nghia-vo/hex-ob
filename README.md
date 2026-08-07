# hex-ob — HEX Ophyd-async + Bluesky for Dev

Workspace for developing and testing **hextools** and **hex-profile-collection**
against the simulated HEX beamline
([`hxm_program/simulated_beamlines/HEX`](https://github.com/NSLS2/hxm_program/tree/main/simulated_beamlines/HEX))
without touching the production repos (Nghia's setup, 2026-07-30). Break things
freely here; merge back when green.

## Layout

```
hex-ob/
  hex-simulated-beamline/   # tracked — the simulated HEX beamline (canonical home)
  hextools/                 # full clone (own .git, all branches, GitHub remotes)
  hex-profile-collection/   # full clone (own .git, all branches, GitHub remotes)
  README.md                 # this file — sandbox-level docs live at this level
```

**The simulated beamline moved here from `hxm_program/simulated_beamlines/HEX`
(2026-08-07)** — hex-ob is now its canonical home, so a PR's own SHA carries
both the code under test and the sim that validates it. Machine-local state
(`.toolenv/` helper venv, `.reflow2/`) is gitignored; recreate the venv via
`hex-simulated-beamline/scripts/up_all.sh` (it bootstraps on first run).

The two package directories are **independent nested git repositories**,
deliberately ignored by hex-ob's own git (see `.gitignore`). hex-ob tracks only
sandbox-level documentation/glue; the packages keep their own history so the
path back to production stays clean.

## The contract

1. **Develop here.** Work on branches inside `hextools/` / `hex-profile-collection/`
   (current dev branches: `hxm1288-f1-common-scaffold`, `hxm-1288-sim-boot`).
   Guided-tutorial work for HXM-1288 tomography (AJ implements, agent reviews)
   happens in these copies.
2. **Test against the sim.** The simulated beamline (PandA `Tomo_radio_1_config`,
   real AD IOC frame tier, motor + bridge) is the test bed; the pyepics
   `tomo_flyscan.py` oracle outputs are the functional-equivalence reference
   (DECISIONS.md D-0014).
3. **Merge back via PRs.** When something is production-ready, push its branch
   to the fork remote and open a PR against the NSLS2 repo (AJ authors, Nghia
   reviews/merges — D-0013). Nothing lands in production by side effect.

## Environments

Pixi/venv environments were deliberately NOT copied (they embed absolute
paths). Recreate in place:

```bash
cd hex-profile-collection && pixi install     # profile envs
cd hextools && uv sync                        # hextools (develop against uv.lock)
```
