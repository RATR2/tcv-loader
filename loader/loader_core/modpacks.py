"""Discovers modpacks under modpacks/, tracks each one's enabled/priority
state for the GUI, and persists that state to loadorder.json in the same
format modpacks/combiner/combine.py reads.

This module owns *state* (what's on, in what order) so the GUI has
something to show and toggle. It deliberately does not reimplement the
merge algorithm: for anything that actually needs to combine patches
(the "will this load order work" preview), it loads combine.py the same
way install.py loads install_mod.py, and calls into it.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType


@dataclass
class ModpackInfo:
    id: str                     # manifest meta.id, falling back to the folder name
    name: str                   # display name (manifest meta.name, or the folder name)
    dir: str                    # "<folder>/<manifest file>", relative to modpacks/
    root: Path = field(default_factory=Path)  # the modpack's own folder, for local meta.image
    manifest: dict = field(default_factory=dict)
    enabled: bool = False
    priority: int = 0

    @property
    def forced(self) -> bool:
        """Mirrors combine.py's load_order(): a pack with meta.forced gets
        folded into every build regardless of loadorder.json, so the GUI
        should show it as always-on rather than a checkbox that lies."""
        return bool(self.manifest.get("meta", {}).get("forced", False))

    @property
    def image_ref(self) -> str:
        return self.manifest.get("meta", {}).get("image", "")

    def local_image_path(self) -> Path | None:
        """None for an empty field or an http(s) URL (nothing to resolve;
        the browser side loads those directly); the resolved absolute path
        for a modpack-local file like Fabric's icon.png, unverified for
        existence here (the caller decides how to handle a missing file)."""
        ref = self.image_ref
        if not ref or ref.startswith(("http://", "https://")):
            return None
        return self.root / ref


def _load_combine(modpacks_dir: Path) -> ModuleType:
    """combine.py loaded fresh each call, the same trade-off install.py
    makes for install_mod.py: a GUI session always runs against whatever
    is on disk right now."""
    path = modpacks_dir / "combiner" / "combine.py"
    spec = importlib.util.spec_from_file_location("combine", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["combine"] = module
    spec.loader.exec_module(module)
    return module


def discover(modpacks_dir: Path) -> dict[str, ModpackInfo]:
    """One entry per manifest *.json sitting directly inside a modpack's own
    folder (Multiplayer/multiplayer.json, Audio-Playback-dub/dubpackaudplayback.json,
    ...). The combiner/ folder holds loadorder.json and combine.py, not a
    modpack, so it's skipped."""
    packs: dict[str, ModpackInfo] = {}
    if not modpacks_dir.is_dir():
        return packs
    for folder in sorted(p for p in modpacks_dir.iterdir() if p.is_dir()):
        if folder.name == "combiner":
            continue
        for manifest_path in sorted(folder.glob("*.json")):
            try:
                manifest = json.loads(manifest_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            meta = manifest.get("meta", {})
            pack_id = meta.get("id") or folder.name
            packs[pack_id] = ModpackInfo(
                id=pack_id,
                name=meta.get("name", folder.name),
                dir=f"{folder.name}/{manifest_path.name}",
                root=folder,
                manifest=manifest,
            )
    return packs


def load_loadorder(path: Path) -> dict:
    if not path.is_file():
        return {"meta": {}, "modpacks": {}}
    return json.loads(path.read_text())


def apply_loadorder_state(packs: dict[str, ModpackInfo], order: dict) -> None:
    """Marks each discovered pack enabled (with its saved priority) if
    loadorder.json lists it, disabled otherwise. Matched by dir, case
    insensitively, since the repo's own loadorder.json has at least one entry
    whose capitalisation doesn't match the folder on disk (see combine.py's
    resolve_insensitive()), and this should tolerate that the same way."""
    by_dir = {entry.get("dir", "").lower(): entry
              for entry in order.get("modpacks", {}).values()}
    for pack in packs.values():
        entry = by_dir.get(pack.dir.lower())
        if entry is None:
            pack.enabled = pack.forced
            pack.priority = 0
        else:
            pack.enabled = True
            pack.priority = int(entry.get("priority", 0))


def list_for_ui(packs: dict[str, ModpackInfo]) -> list[dict]:
    out = [
        {
            "id": p.id,
            "name": p.name,
            "description": p.manifest.get("meta", {}).get("description", ""),
            "author": p.manifest.get("meta", {}).get("author", ""),
            "version": p.manifest.get("meta", {}).get("version", ""),
            # Raw manifest value, not yet an <img>-usable src; empty means no icon,
            # else call get_pack_image(id) (local paths become data: URIs; file:// isn't reliable across pywebview backends).
            "image": p.image_ref,
            "game_versions": p.manifest.get("meta", {}).get("game_version", []),
            "modloader": p.manifest.get("meta", {}).get("modloader", ""),
            "enabled": p.enabled,
            "priority": p.priority,
            "forced": p.forced,
        }
        for p in packs.values()
    ]
    out.sort(key=lambda d: (not d["enabled"], d["priority"], d["name"].lower()))
    return out


def move_pack(packs: dict[str, ModpackInfo], pack_id: str, direction: int) -> None:
    """Swaps pack_id with its neighbour among the *enabled, non-forced*
    packs, ordered by priority, then renumbers that group 1..N to match.
    A forced pack's priority is fixed at 0 by apply_loadorder_state() and it
    never appears in this ordering (there's nothing for a user to reorder,
    see ModpackInfo.forced). direction is -1 (move earlier/loads sooner) or
    +1 (move later)."""
    pack = packs.get(pack_id)
    if pack is None or not pack.enabled or pack.forced:
        return
    ordered = sorted(
        (p for p in packs.values() if p.enabled and not p.forced),
        key=lambda p: (p.priority, p.name.lower()))
    index = next((i for i, p in enumerate(ordered) if p.id == pack_id), None)
    target = index + direction if index is not None else None
    if index is None or target is None or not (0 <= target < len(ordered)):
        return
    ordered[index], ordered[target] = ordered[target], ordered[index]
    for new_priority, p in enumerate(ordered, start=1):
        p.priority = new_priority


def save_loadorder(path: Path, packs: dict[str, ModpackInfo]) -> None:
    order = {
        "meta": {"description": "Lower priority numbers load first.",
                  "allow_duplicates": False},
        "modpacks": {
            p.name: {"priority": p.priority, "dir": p.dir}
            for p in packs.values() if p.enabled
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(order, indent=2) + "\n")


def preview_conflicts(modpacks_dir: Path, loadorder_path: Path, game_version: str) -> dict:
    """Dry-runs combine.build() into a throwaway directory so the "check
    conflicts" button can report whether the current load order will merge
    cleanly for game_version, without touching any real build output."""
    combine = _load_combine(modpacks_dir)
    version_key = game_version.replace(".", "_")
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "preview"
        try:
            combine.build(version_key, out_dir, loadorder_path)
        except combine.CombineError as exc:
            return {"ok": False, "error": str(exc)}
        manifest = json.loads((out_dir / "build_manifest.json").read_text())
    return {
        "ok": True,
        "modpacks": manifest.get("modpacks", []),
        "injected_code": manifest.get("injected_code", []),
    }
