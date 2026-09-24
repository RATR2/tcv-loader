"""Runs the full pipeline: check the game copy, decompile it, combine the
enabled modpacks for its detected version, patch them in, and export a
modded build, reporting progress through a callback instead of print(), so
a GUI can drive a real status display instead of scraping stdout.

Everything that actually talks to gdRE/Godot/Defender comes from
loader_core.engine, a plain sibling module in this package; there is no
separate command-line installer any more, so nothing here needs the dynamic
importlib trick combine.py still gets (that one genuinely lives outside the
package, under modpacks/combiner/). The one thing engine.py doesn't know
about is how the mod itself gets built: it has no notion of "the mod" at
all any more. That's this module's job, via apply_combined_mod(), which
calls modpacks/combiner/combine.py to merge whatever the user enabled in
loadorder.json for the game version actually detected.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Callable

from loader_core import bundled_resource, engine, repo_root

REPO_ROOT = repo_root()
MODPACKS_DIR = REPO_ROOT / "modpacks"
LOADORDER_PATH = MODPACKS_DIR / "combiner" / "loadorder.json"
COMBINE_PATH = MODPACKS_DIR / "combiner" / "combine.py"
EXPORT_PRESET_TEMPLATE = bundled_resource("export_presets_template.cfg")

# No version suffix (unlike engine.OUTPUT_STEM): replaced every install, not versioned; see export_copy() below to save one out first.
OUTPUT_STEM = "TheChoicerVoicer-Modded"

# Provisional hardcoded mapping (manifest schema has no injected-file-is-an-autoload key yet); only applies if the file is actually in this build's merged injected_code.
PROVISIONAL_AUTOLOADS: dict[str, tuple[str, str]] = {
    "net/net_manager.gd": ("Net", "res://net/net_manager.gd"),
    "modmenu/mod_menu_manager.gd": ("ModMenuManager", "res://modmenu/mod_menu_manager.gd"),
}

AUTOLOAD_ANCHOR = 'Metro="*res://common/globals/metro.gd"'
LOG_ANCHOR = "settings/stdout/verbose_stdout=true"
LOG_LINES = ['file_logging/enable_file_logging=true',
             'file_logging/log_path="user://logs/choicervoicer.log"',
             'file_logging/max_log_files=20']

ProgressFn = Callable[[str, str], None]


def _noop_progress(phase: str, message: str) -> None:
    pass


def _combine() -> ModuleType:
    """combine.py loaded fresh each call, so a GUI session always runs
    against whatever is on disk right now rather than a stale cached copy
    from when the app started. It lives under modpacks/combiner/, outside
    this package, which is the actual reason this still needs the dynamic
    importlib trick; engine.py doesn't, since it's a normal sibling
    module."""
    spec = importlib.util.spec_from_file_location("combine", COMBINE_PATH)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(COMBINE_PATH)
    module = importlib.util.module_from_spec(spec)
    # @dataclass looks its class up in sys.modules by __module__ name; exec_module() doesn't
    # register it there, so without this, import fails with a cryptic AttributeError.
    sys.modules["combine"] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class InstallResult:
    output: Path
    size_bytes: int
    game_version: str
    modpacks: list[dict] = field(default_factory=list)


def find_game_candidates() -> list[Path]:
    """Non-interactive equivalent of engine.choose_game_exe() (there is no
    such function here, on purpose): returns candidates for a GUI picker to
    show instead of prompting on stdin, which has nothing to read from in a
    GUI process."""
    return engine.find_game_exe()


def register_autoloads(work: Path, autoloads: dict[str, str],
                        on_progress: ProgressFn = _noop_progress) -> None:
    """Insert zero or more autoloads into project.godot, anchored after the
    game's own Metro autoload, plus the verbose file-logging lines this mod
    has always turned on. A generalisation of the old single-mod installer's
    equivalent, which only ever knew how to insert exactly one hardcoded
    line and raised if it was already there, not workable once the set of
    mods (and so the set of autoloads) varies per install."""
    path = work / "project.godot"
    text = path.read_text(encoding="utf-8")

    to_add = [f'{name}="*{res}"' for name, res in autoloads.items()
              if f'{name}="*{res}"' not in text]
    if to_add:
        if AUTOLOAD_ANCHOR not in text:
            raise engine.Failed(
                "project.godot has no Metro autoload to anchor to; "
                "unexpected game version")
        text = text.replace(AUTOLOAD_ANCHOR,
                             AUTOLOAD_ANCHOR + "\n" + "\n".join(to_add), 1)
        on_progress("autoload", f"registered {', '.join(sorted(autoloads))}")

    if LOG_ANCHOR in text and LOG_LINES[0] not in text:
        text = text.replace(LOG_ANCHOR, "\n".join([LOG_ANCHOR] + LOG_LINES), 1)

    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def write_export_preset(work: Path) -> None:
    """EXPORT_PRESET_TEMPLATE has engine.OUTPUT_STEM baked into its
    export_path values (it's the file the original single-mod export preset
    shipped with); swap that for this pipeline's own fixed OUTPUT_STEM,
    for every platform in one pass."""
    text = EXPORT_PRESET_TEMPLATE.read_text(encoding="utf-8")
    for _platform, info in engine.TARGET_PLATFORMS.items():
        suffix = info["output_suffix"]
        text = text.replace(f'export_path="{engine.OUTPUT_STEM}{suffix}"',
                             f'export_path="{OUTPUT_STEM}{suffix}"', 1)
    with open(work / "export_presets.cfg", "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def apply_combined_mod(work: Path, game_version_dotted: str,
                        on_progress: ProgressFn = _noop_progress) -> dict:
    """Builds whatever modpacks loadorder.json enables for this game version
    with combine.py, applies the result to a decompiled project, and returns
    the build manifest (which modpacks were used, and what got injected)."""
    combine = _combine()
    version_key = game_version_dotted.replace(".", "_")

    on_progress("repair", "fixing decompiler node-path artifacts")
    fixed = engine.repair_node_paths(work)
    on_progress("repair", f"repaired {fixed} decompiler node-path artifact(s)")

    build_dir = work.parent / f"{work.name}-combined-mod"
    shutil.rmtree(build_dir, ignore_errors=True)
    on_progress("combine", f"combining enabled modpacks for {game_version_dotted}")
    try:
        combine.build(version_key, build_dir, LOADORDER_PATH)
    except combine.CombineError as exc:
        raise engine.Failed(f"combining the enabled modpacks failed:\n{exc}")
    manifest = json.loads((build_dir / "build_manifest.json").read_text())

    patches_dir = build_dir / "patches" / f"v{version_key}"
    count = 0
    for patch in sorted(patches_dir.glob("*.patch")):
        rel = patch.name[: -len(".patch")].replace("__", "/")
        target = work / rel
        if not target.is_file():
            raise engine.Failed(f"expected game file missing: {rel}")
        engine.apply_patch(target, patch.read_text(encoding="utf-8"), game_version_dotted)
        count += 1
    on_progress("patch", f"applied {count} patched game script(s)")

    injected = manifest.get("injected_code", [])
    for rel in injected:
        source = build_dir / rel
        dest = work / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    if injected:
        on_progress("inject", f"added {len(injected)} file(s) ({', '.join(injected[:3])}"
                    f"{', ...' if len(injected) > 3 else ''})")

    # build_dir (and combine.py's copied images) gets thrown away below, so bundle it into
    # the exported game itself (res://mods_manifest.json) for an in-game mod menu to read.
    shutil.copy2(build_dir / "build_manifest.json", work / "mods_manifest.json")
    images_dir = build_dir / "modpack_images"
    if images_dir.is_dir():
        shutil.copytree(images_dir, work / "modpack_images", dirs_exist_ok=True)

    autoloads = {name: res for rel, (name, res) in PROVISIONAL_AUTOLOADS.items()
                 if rel in injected}
    register_autoloads(work, autoloads, on_progress)
    write_export_preset(work)

    shutil.rmtree(build_dir, ignore_errors=True)
    return manifest


def install(exe: Path, *, output: Path | None = None, cache: Path | None = None,
            work: Path | None = None, keep_work: bool = False,
            on_progress: ProgressFn = _noop_progress) -> InstallResult:
    engine.start_run_log()  # best-effort; every install gets a log under install_logs/
    cache = Path(cache) if cache else (REPO_ROOT / ".cache")
    work = Path(work) if work else (REPO_ROOT / "work")
    exe = Path(exe)

    on_progress("check", f"checking {exe.name}")
    target = engine.check_game_exe(exe)

    on_progress("gdre", "getting gdRE Tools")
    gdre = engine.get_gdre(cache, None)

    on_progress("decompile", "decompiling your copy of the game")
    engine.decompile(gdre, exe, work)

    game_version = engine.detect_version(work)
    on_progress("version", f"project reports version {game_version}")

    manifest = apply_combined_mod(work, game_version, on_progress)

    on_progress("godot", "getting Godot and export templates")
    godot = engine.get_godot(cache, None)
    engine.ensure_templates(cache, target)

    suffix = engine.TARGET_PLATFORMS[target]["output_suffix"]
    out_path = Path(output) if output else (REPO_ROOT / f"{OUTPUT_STEM}{suffix}")
    if out_path.exists():
        out_path.unlink()  # this build always replaces the last one

    on_progress("export", f"exporting to {out_path.name}")
    engine.export(godot, work, out_path, target)

    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)

    size = out_path.stat().st_size
    on_progress("done", f"built {out_path.name} ({size // 1048576} MB)")
    return InstallResult(output=out_path, size_bytes=size,
                          game_version=game_version,
                          modpacks=manifest.get("modpacks", []))


def export_copy(built: Path, destination: Path) -> Path:
    """"if a user wants they can export it somewhere else": install()
    always overwrites the one fixed-name build, so saving a particular one
    out from under that is just a plain copy taken before the next install."""
    built = Path(built)
    destination = Path(destination)
    if not built.is_file():
        raise FileNotFoundError(f"nothing built yet at {built}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, destination)
    return destination


def launch(output: Path) -> None:
    """Starts the modded build detached from this process, so closing the
    loader doesn't take the game down with it (and, on Windows, so the
    child's own console/window isn't tied to a hidden parent)."""
    output = Path(output)
    if not output.is_file():
        raise FileNotFoundError(f"nothing built at {output}")
    if sys.platform == "win32":
        # DETACHED_PROCESS: no console inherited. CREATE_NEW_PROCESS_GROUP:
        # so closing the loader's own process group doesn't signal this one.
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([str(output)], cwd=str(output.parent),
                          creationflags=flags, close_fds=True)
    elif sys.platform == "darwin" and output.suffix == ".app":
        subprocess.Popen(["open", str(output)], start_new_session=True)
    else:
        subprocess.Popen([str(output)], cwd=str(output.parent),
                          start_new_session=True, close_fds=True)
