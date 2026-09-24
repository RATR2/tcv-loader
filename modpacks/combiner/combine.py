#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# Bounds for a modpack's meta.image, so a huge or absurdly large icon can't
# bloat a build or hand the game an image big enough to hang or crash on
# decode/GPU upload. Local images are checked here at build time; the
# in-game mod menu (mod_menu_overlay.gd) enforces the same idea at runtime
# for http(s) images, since those never pass through this process at all.
MAX_IMAGE_BYTES = 512 * 1024
MAX_IMAGE_DIMENSION = 512

HERE = Path(__file__).resolve().parent
MODPACKS = HERE.parent
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class CombineError(Exception):
    pass


# - modpack loading


@dataclass
class Modpack:
    name: str
    priority: int
    root: Path
    manifest: dict
    patches: list[Path] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.manifest.get("meta", {}).get("id", self.name)

    @property
    def game_versions(self) -> list[str]:
        return self.manifest.get("meta", {}).get("game_version", [])


def load_order(path: Path) -> list[Modpack]:
    base = path.resolve().parent
    if base.name == "combiner":
        base = base.parent
    data = json.loads(path.read_text())
    meta = data.get("meta", {})
    allow_duplicates = bool(meta.get("allow_duplicates", False))

    packs: list[Modpack] = []
    seen_manifests: set[str] = set()
    for name, entry in data.get("modpacks", {}).items():
        rel = entry["dir"]
        manifest_path = resolve_insensitive(base, rel)
        if manifest_path is None:
            raise CombineError(f"{name}: no manifest at {base}/{rel}")
        packs.append(Modpack(
            name=name,
            priority=int(entry.get("priority", 0)),
            root=manifest_path.parent,
            manifest=json.loads(manifest_path.read_text()),
        ))
        seen_manifests.add(str(manifest_path.resolve()).lower())

    # meta.forced folds a pack in even if loadorder.json never mentions it (hand-edited, stale,
    # or predates this key), so leaving it out isn't a way to disable it. Still gated by game_version like everything else, further down in build().
    for manifest_path in sorted(base.glob("*/*.json")):
        if manifest_path.parent.name == "combiner":
            continue
        if str(manifest_path.resolve()).lower() in seen_manifests:
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not manifest.get("meta", {}).get("forced", False):
            continue
        packs.append(Modpack(
            name=manifest.get("meta", {}).get("name", manifest_path.parent.name),
            priority=0,
            root=manifest_path.parent,
            manifest=manifest,
        ))

    packs.sort(key=lambda p: (p.priority, p.name))

    if not allow_duplicates:
        seen: dict[str, str] = {}
        for pack in packs:
            if pack.id in seen:
                raise CombineError(
                    f"two modpacks share the id {pack.id!r}: "
                    f"{seen[pack.id]} and {pack.name}. Set allow_duplicates "
                    "in loadorder.json if this is deliberate.")
            seen[pack.id] = pack.name
    return packs


def resolve_insensitive(base: Path, rel: str) -> Path | None:
    """loadorder.json is hand-written and the repo has at least one entry whose
    capitalisation does not match the folder on disk. That is invisible on
    Windows and fatal on Linux, so match case-insensitively and carry on."""
    current = base
    for part in Path(rel).parts:
        if (current / part).exists():
            current = current / part
            continue
        matches = [c for c in current.iterdir() if c.name.lower() == part.lower()]
        if len(matches) != 1:
            return None
        current = matches[0]
    return current


def patches_for(pack: Modpack, version: str) -> list[Path]:
    """A modpack lists patches either flat under "patches", or split into
    "common_patches" plus "version_patches" when one file differs per game
    build. Both resolve to a list of real paths under <pack>/patches/."""
    manifest = pack.manifest
    base = pack.root / "patches"
    out: list[Path] = []

    for rel in manifest.get("common_patches", []):
        out.append(base / "common" / rel)
    for rel, want in manifest.get("version_patches", {}).items():
        if want == version:
            out.append(base / rel)
    for rel in manifest.get("patches", []):
        candidate = base / rel
        if not candidate.is_file():
            candidate = base / f"v{version}" / rel
        out.append(candidate)

    missing = [p for p in out if not p.is_file()]
    if missing:
        raise CombineError(
            f"{pack.name}: manifest lists patches that do not exist:\n  " +
            "\n  ".join(str(p) for p in missing))
    return out


def target_of(patch: Path) -> str:
    """patches use __ where the game's res:// path uses /, matching the naming
    install_mod.py already expects."""
    return patch.name[: -len(".patch")].replace("__", "/")


def image_dimensions(path: Path) -> tuple[int, int] | None:
    """Best-effort width/height for PNG and JPEG without pulling in a
    dependency like Pillow just for this. None for anything else (WebP,
    SVG, ...) or a file that doesn't parse as either. Callers should treat
    that as "couldn't check", not "has no size", and skip the pixel check
    rather than reject a format this doesn't happen to read."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return width, height
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                height, width = struct.unpack(">HH", data[i + 5:i + 9])
                return width, height
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + length
        return None
    return None


def resolve_image(pack: Modpack, out_dir: Path) -> str:
    """meta.image is either an http(s) URL (left as-is, fetched at runtime by
    whatever displays it) or a path to a file the modpack ships, relative to
    its own manifest folder (like a Fabric mod's icon.png). A local one gets
    copied into the build under modpack_images/<id>/ and the returned value
    points at where it will actually live once copied into a Godot project,
    since nothing downstream should need to know modpacks/ existed."""
    image = pack.manifest.get("meta", {}).get("image", "")
    if not image or image.startswith(("http://", "https://")):
        return image
    src = pack.root / image
    if not src.is_file():
        raise CombineError(f"{pack.name}: meta.image {image!r} does not exist at {src}")
    size = src.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise CombineError(
            f"{pack.name}: meta.image {image!r} is {size // 1024} KB, over the "
            f"{MAX_IMAGE_BYTES // 1024} KB limit for a mod icon.")
    dims = image_dimensions(src)
    if dims and (dims[0] > MAX_IMAGE_DIMENSION or dims[1] > MAX_IMAGE_DIMENSION):
        raise CombineError(
            f"{pack.name}: meta.image {image!r} is {dims[0]}x{dims[1]}, over the "
            f"{MAX_IMAGE_DIMENSION}x{MAX_IMAGE_DIMENSION} limit for a mod icon.")
    dest_rel = f"modpack_images/{pack.id}/{src.name}"
    dest = out_dir / dest_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return f"res://{dest_rel}"


# - decomposing patches into original-coordinate edits

@dataclass
class Edit:
    """One mod's changes to one game file, in pristine-file line numbers."""
    pack: str
    priority: int
    inserts: dict[int, list[str]] = field(default_factory=dict)
    deletes: dict[int, str] = field(default_factory=dict)
    known: dict[int, str] = field(default_factory=dict)


def decompose(patch_text: str, pack: str, priority: int) -> Edit:
    edit = Edit(pack=pack, priority=priority)
    lines = patch_text.replace("\r\n", "\n").split("\n")
    # Drop the spurious "" from a trailing \n; else it reads as an extra blank line.
    if lines and lines[-1] == "":
        lines.pop()
    i = 0
    while i < len(lines):
        head = HUNK_RE.match(lines[i])
        if not head:
            i += 1
            continue
        orig = int(head.group(1))
        i += 1
        pending: list[str] = []
        while i < len(lines) and not lines[i].startswith(("@@", "--- ", "+++ ")):
            raw = lines[i]
            i += 1
            if raw.startswith("\\"):
                continue
            tag, content = raw[:1], raw[1:]
            if tag == "+":
                pending.append(content)
                continue
            if pending:
                edit.inserts.setdefault(orig, []).extend(pending)
                pending = []
            if tag == "-":
                edit.deletes[orig] = content
                edit.known[orig] = content
            elif tag == " " or raw == "":
                edit.known[orig] = content if tag == " " else ""
            else:
                continue
            orig += 1
        if pending:
            edit.inserts.setdefault(orig, []).extend(pending)
    return edit

# - merging

def merge(edits: list[Edit], target: str) -> tuple[dict[int, str], dict[int, list[str]], set[int]]:
    known: dict[int, str] = {}
    provenance: dict[int, str] = {}
    for edit in edits:
        for line_no, content in edit.known.items():
            if line_no in known and known[line_no] != content:
                raise CombineError(
                    f"{target}: {edit.pack} and {provenance[line_no]} disagree "
                    f"about line {line_no} of the unmodified file.\n"
                    f"  {provenance[line_no]:<24} {known[line_no]!r}\n"
                    f"  {edit.pack:<24} {content!r}\n"
                    "  One of these was made against a different game version.")
            known[line_no] = content
            provenance[line_no] = edit.pack

    deletes: set[int] = set()
    deleted_by: dict[int, str] = {}
    for edit in edits:
        for line_no in edit.deletes:
            if line_no in deletes:
                raise CombineError(
                    f"{target}: {edit.pack} and {deleted_by[line_no]} both "
                    f"replace line {line_no} ({known[line_no]!r}).\n"
                    "  Two mods cannot rewrite the same line; they need "
                    "merging by hand or a priority decision.")
            deletes.add(line_no)
            deleted_by[line_no] = edit.pack

    inserts: dict[int, list[str]] = defaultdict(list)
    for edit in sorted(edits, key=lambda e: e.priority):
        for line_no, block in edit.inserts.items():
            inserts[line_no].extend(block)
    return known, dict(inserts), deletes


# - emitting


def emit(target: str, known: dict[int, str], inserts: dict[int, list[str]],
         deletes: set[int], context: int = 3) -> str:
    touched = sorted(set(inserts) | deletes)
    if not touched:
        return ""

    groups: list[list[int]] = []
    for line_no in touched:
        if groups and line_no - groups[-1][-1] <= context * 2:
            groups[-1].append(line_no)
        else:
            groups.append([line_no])

    out = [f"--- a/{target}", f"+++ b/{target}"]
    shift = 0
    for group in groups:
        lo, hi = group[0] - context, group[-1] + context
        while lo > 1 and lo not in known:
            lo += 1
        while hi >= lo and hi not in known:
            hi -= 1
        while any(n not in known for n in range(lo, hi + 1)):
            for n in range(lo, hi + 1):
                if n not in known:
                    hi = n - 1
                    break
        body: list[str] = []
        for line_no in range(lo, hi + 1):
            for added in inserts.get(line_no, []):
                body.append("+" + added)
            body.append(("-" if line_no in deletes else " ") + known[line_no])
        for added in inserts.get(hi + 1, []):
            body.append("+" + added)

        old_count = sum(1 for ln in body if not ln.startswith("+"))
        new_count = sum(1 for ln in body if not ln.startswith("-"))
        out.append(f"@@ -{lo},{old_count} +{lo + shift},{new_count} @@")
        out.extend(body)
        shift += new_count - old_count
    return "\n".join(out) + "\n"


# - build


def build(version: str, out_dir: Path, order_path: Path) -> None:
    packs = load_order(order_path)
    if not packs:
        raise CombineError("loadorder.json lists no modpacks")

    usable: list[Modpack] = []
    for pack in packs:
        if pack.game_versions and version not in pack.game_versions:
            print(f"  skip  {pack.name}: does not support {version}")
            continue
        pack.patches = patches_for(pack, version)
        usable.append(pack)
        print(f"  load  {pack.name} (priority {pack.priority}, "
              f"{len(pack.patches)} patches)")
    if not usable:
        raise CombineError(f"no modpack in the load order supports {version}")

    by_target: dict[str, list[Edit]] = defaultdict(list)
    for pack in usable:
        for patch in pack.patches:
            by_target[target_of(patch)].append(
                decompose(patch.read_text(), pack.name, pack.priority))

    patches_out = out_dir / "patches" / f"v{version}"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    patches_out.mkdir(parents=True)

    for target, edits in sorted(by_target.items()):
        known, inserts, deletes = merge(edits, target)
        text = emit(target, known, inserts, deletes)
        if not text:
            continue
        name = target.replace("/", "__") + ".patch"
        (patches_out / name).write_text(text)
        who = ", ".join(sorted({e.pack for e in edits}))
        print(f"  merge {target}  <- {who}")

    injected: dict[str, str] = {}
    for pack in usable:
        patches_root = pack.root / "patches"
        for pattern in pack.manifest.get("injected_code", []):
            matches = sorted(p for p in patches_root.glob(pattern) if p.is_file())
            if not matches:
                raise CombineError(
                    f"{pack.name}: injected_code pattern {pattern!r} matched "
                    f"no files under {patches_root}")
            for source in matches:
                rel = source.relative_to(patches_root).as_posix()
                if rel in injected and injected[rel] != pack.name:
                    raise CombineError(
                        f"{pack.name} and {injected[rel]} both inject {rel}")
                dest = out_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
                injected[rel] = pack.name

    (out_dir / "build_manifest.json").write_text(json.dumps({
        "game_version": version,
        "modpacks": [
            {"name": p.name, "id": p.id, "priority": p.priority,
             "version": p.manifest.get("meta", {}).get("version", ""),
             "author": p.manifest.get("meta", {}).get("author", ""),
             "description": p.manifest.get("meta", {}).get("description", ""),
             "image": resolve_image(p, out_dir),
             "links": [v for k, v in p.manifest.get("meta", {}).items()
                       if k.startswith("link") and v]}
            for p in usable
        ],
        "injected_code": sorted(injected),
    }, indent=2) + "\n")
    print(f"\nbuilt {out_dir} for game {version} "
          f"from {len(usable)} modpack(s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--game-version", default="0_5_3")
    ap.add_argument("--out", type=Path, default=HERE.parent.parent / "build" / "mod")
    ap.add_argument("--loadorder", type=Path, default=HERE / "loadorder.json")
    args = ap.parse_args()
    try:
        build(args.game_version, args.out.resolve(), args.loadorder)
    except CombineError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
