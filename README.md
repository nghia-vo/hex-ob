# hex-ob — HEX Ophyd-async + Bluesky for Dev

Workspace for developing and testing **hextools** and **hex-profile-collection**
against the simulated HEX beamline
([`hxm_program/simulated_beamlines/HEX`](https://github.com/NSLS2/hxm_program/tree/main/simulated_beamlines/HEX))
without touching the production repos (Nghia's setup, 2026-07-30). Break things
freely here; merge back when green.

## Layout

```
hex-ob/
  lib/                      # tracked — HEX device definitions (ophyd-async)
  plans/                    # tracked — Bluesky plans (tomography/, ...)
  TUTORIAL.md               # tracked — scientist-facing how-to (Nghia)
  tests/                    # tracked — mock + sim verification scripts
  hextools/                 # full clone (own .git, all branches, GitHub remotes)
  hex-profile-collection/   # full clone (own .git, all branches, GitHub remotes)
  README.md                 # this file — sandbox-level docs live at this level
```

Since 2026-08-02 (Nghia), the **working code for the HXM-1288 port lives in the
tracked top-level `lib/` + `plans/`** and is developed/PRed in this repo
directly. The two package directories remain **independent nested git
repositories**, deliberately ignored by hex-ob's own git (see `.gitignore`) —
they hold parked branches and the profile/pixi environment used to run against
the sim; promotion of `lib/`/`plans/` into `hextools` proper is deferred until
the code is beamline-proven.

## The contract

1. **Develop here.** Working code goes in top-level `lib/` + `plans/` on a
   branch. The coding agent implements; AJ reviews. (The guided-tutorial
   learning track lives elsewhere — hxm_program's shared bluesky-daq-tutorial.)
   Target ophyd-async **0.19.x** — the version pinned by
   `hex-profile-collection/pixi.toml`; run everything through that pixi env.
2. **Test against the sim.** The simulated beamline (PandA `Tomo_radio_1_config`,
   real AD IOC frame tier, motor + bridge) is the test bed; the pyepics
   `tomo_flyscan.py` oracle outputs are the functional-equivalence reference
   (DECISIONS.md D-0014). Each major step is confirmed on the sim, then on the
   real beamline, before it merges.
3. **Merge back via PRs.** Sim-verified + beamline-verified branches are PRed
   against `NSLS2/hex-ob` `main` (AJ authors, Nghia reviews/merges). For the
   nested clones the old contract still applies: push to the fork remote, PR
   to the NSLS2 repo. Nothing lands in production by side effect.

## Environments

Pixi/venv environments were deliberately NOT copied (they embed absolute
paths). Recreate in place:

```bash
cd hex-profile-collection && pixi install     # profile envs
cd hextools && uv sync                        # hextools (develop against uv.lock)
```
