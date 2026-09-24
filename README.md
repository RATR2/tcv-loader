<div align="center">

# TCV Modloader

[![Issues](https://img.shields.io/github/issues/RATR2/TCV-Loader?style=for-the-badge&logo=github)](https://github.com/RATR2/TCV-Loader/issues)
[![Stars](https://img.shields.io/github/stars/RATR2/TCV-Loader?style=for-the-badge&logo=github)](https://github.com/RATR2/TCV-Loader/stargazers)
[![Game](https://img.shields.io/badge/game-0.5.1%20%7C%200.5.2%20%7C%200.5.3-blue?style=for-the-badge)](#supported-game-builds)
[![Platforms](https://img.shields.io/badge/platforms-windows%20%7C%20linux-blue?style=for-the-badge)](#supported-game-builds)
[![Status](https://img.shields.io/badge/status-in--development-yellow?style=for-the-badge)](#roadmap)

> Most Godot mods mean hand-editing a decompile and hoping your changes still line up after the next game update.
> TCV Loader flips that around: mods are patches with a manifest, merged fresh against your own game copy every single build.

If you find any issues, please report them [here](https://github.com/RATR2/TCV-Loader/issues)

</div>

---

## What is this

TCV Loader builds a modded copy of *The Choicer Voicer* from a game `.exe`/`.x86_64` you already own. Point it at your copy, tick the mods you want, hit **Install & Launch**: it decompiles your game, merges whatever's enabled into one patch set, applies it, and exports a working modded build next to itself. No game files ship in this repo, and nothing gets downloaded on your behalf except the free, official tools needed to rebuild your own copy: gdRE Tools and Godot itself.

It comes with three mods out of the box: **TCV-Multiplayer** (play the normal game show or dub mode with up to 4 people online), **DubPack Playback** (hear your own take played back after "Hear clip again"), and **Mod Menu** (an in-game button listing whatever's actually loaded in the build you're running).

## Why not just patch the decompile by hand

That's genuinely how this started: a single script that decompiled the game and applied one hardcoded set of patches. It worked, until there was a second mod:

- **One mod's patch doesn't know another mod exists.** Two mods editing the same file by hand means manually splicing diffs together every time either one changes.
- **A silent conflict is worse than no merge at all.** The combiner decomposes every patch to the exact original lines it touches; two mods disagreeing about a line (not just overlapping, actually disagreeing) fails the build with both mods named and the line quoted, instead of quietly producing whichever one applied last.
- **A single "the mod" script can't express "install these three, not that one."** The loader treats every mod as independent and optional, gated by which game version it declares support for.
- **A decompile drifts.** The exact same nominal game version can still change a line here and there between builds (this happened more than once building this out). Patches are re-applied to a fresh decompile every run rather than shipping a pre-patched project, so a drifted line is a clear "hunk does not match" instead of a silent corruption.

## Quick start

```
./run.sh          # Linux/macOS
run.bat           # Windows
```

Either one sets up a `.venv` (using [uv](https://astral.sh/uv) if it's on your PATH, falling back to the standard library's `venv` + `pip` if not), installs dependencies into it, and launches the desktop app. No separate command-line installer; this is the only way in.

## Project structure

| Path | What |
|---|---|
| `run.sh` / `run.bat` | Set up (or repair) a `.venv` and launch the app. Safe to run every time. |
| `loader/app.py` | The desktop app: a pywebview window, and the API it exposes to the page. |
| `ui/` | The page itself: modpack list, install panel, no server involved. Sits next to `modpacks/`, not under `loader/`, since a packaged build ships it alongside the executable rather than inside it. |
| `loader/loader_core/engine.py` | The actual engine: downloading gdRE/Godot, decompiling, patching, exporting. No notion of "the mod"; that's the layer above. |
| `loader/loader_core/install.py` | Drives `engine.py` and calls `modpacks/combiner/combine.py` to merge whatever's enabled, reporting progress through a callback instead of stdout. |
| `loader/loader_core/modpacks.py` | Discovery, enable/disable, and reorder state for the GUI. |
| `modpacks/` | One folder per mod: a manifest (`meta.json`-style) plus its `patches/`. `modpacks/combiner/` merges whichever ones `loadorder.json` enables into one patch set for a given game version. |
| `dev/tools/` | Maintainer-only scripts: reconstruct a synthetic project from patches and run the pipeline against it with no real game copy needed, split a legacy single-file patch into per-file patches, round-trip-verify a patch. Not needed to build or play. |
| `dev/devtest/` | A throwaway Godot peer project used while developing the netcode. |

## Writing a mod

A mod is a folder under `modpacks/` with a manifest and a `patches/` directory:

```json
{
  "meta": {
    "name": "Your Mod",
    "id": "your_mod",
    "author": "you",
    "description": "what it does",
    "version": "1.0.0",
    "image": "icon.png",
    "game_version": ["0_5_3"]
  },
  "patches": ["scenes__gameplay__dub_mode__main__dub_mode.gd.patch"],
  "injected_code": ["your_mod/*"]
}
```

`patches` are unified diffs against the game's decompiled source, named with `__` where the real path has `/`. `injected_code` copies whole new files in (an autoload script, say) rather than editing an existing one. `image` can be a modpack-local file or an `http(s)` URL, shown both in the desktop list and the in-game Mod Menu. Set `"forced": true` in `meta` if a mod should always be installed regardless of what `loadorder.json` says (Mod Menu does this).

## Supported game builds

| Size (bytes) | Build |
|---|---|
| 211382192 / 211382048 | 0.5.1 compatibility / standard |
| 199462432 / 208463056 / 208462000 | 0.5.2 dev-2 compatibility / compatibility / standard |
| 208741456 / 208741312 | 0.5.3 compatibility / standard (Windows) |
| 214540944 / 214541088 | 0.5.3 compatibility / standard (Linux) |

An exe that isn't one of these still gets tried; it's just flagged as untested rather than rejected.

## Roadmap

Done: the modpack combiner with real conflict detection, the desktop app (mod list, reordering, image support, live build progress), and three working mods across Windows and Linux, 0.5.1 through 0.5.3. Not done yet: a real pass on Windows (everything so far has been built and run on Linux).

## License

[MIT](LICENSE).

---

<div align="center">

Thanks, R4T Out.

</div>
