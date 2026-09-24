from __future__ import annotations

import base64
import json
import mimetypes
import threading
from pathlib import Path

import webview
from webview import FileDialog

from loader_core import install, modpacks, repo_root

REPO_ROOT = repo_root()
MODPACKS_DIR = REPO_ROOT / "modpacks"
LOADORDER_PATH = MODPACKS_DIR / "combiner" / "loadorder.json"
UI_INDEX = REPO_ROOT / "ui" / "index.html"


class Api:

    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._packs: dict[str, modpacks.ModpackInfo] = {}
        self._install_lock = threading.Lock()
        self._installing = False
        self._refresh()

    def _refresh(self) -> None:
        self._packs = modpacks.discover(MODPACKS_DIR)
        order = modpacks.load_loadorder(LOADORDER_PATH)
        modpacks.apply_loadorder_state(self._packs, order)

    def list_modpacks(self) -> list[dict]:
        self._refresh()
        return modpacks.list_for_ui(self._packs)

    def set_enabled(self, pack_id: str, enabled: bool) -> list[dict]:
        pack = self._packs.get(pack_id)
        if pack is None:
            raise ValueError(f"unknown modpack id: {pack_id}")
        pack.enabled = pack.forced or bool(enabled)
        if pack.enabled and pack.priority == 0:
            pack.priority = 1 + max(
                (p.priority for p in self._packs.values() if p.enabled and p is not pack),
                default=0)
        modpacks.save_loadorder(LOADORDER_PATH, self._packs)
        return modpacks.list_for_ui(self._packs)

    def set_priority(self, pack_id: str, priority: int) -> list[dict]:
        pack = self._packs.get(pack_id)
        if pack is None:
            raise ValueError(f"unknown modpack id: {pack_id}")
        pack.priority = int(priority)
        modpacks.save_loadorder(LOADORDER_PATH, self._packs)
        return modpacks.list_for_ui(self._packs)

    def move_pack(self, pack_id: str, direction: int) -> list[dict]:
        """direction: -1 to load earlier, +1 to load later. The load-order
        list is the whole reason priority exists; nobody should have to
        think in raw priority numbers to use it."""
        modpacks.move_pack(self._packs, pack_id, int(direction))
        modpacks.save_loadorder(LOADORDER_PATH, self._packs)
        return modpacks.list_for_ui(self._packs)

    def get_pack_image(self, pack_id: str) -> str | None:
        """A src an <img> tag can use directly: an http(s) URL passes
        through unchanged, a modpack-local file gets read and returned as a
        data: URI. That round-trip is wasteful for a huge image, but these
        are meant to be small mod icons, not photos."""
        pack = self._packs.get(pack_id)
        if pack is None:
            return None
        if pack.image_ref.startswith(("http://", "https://")):
            return pack.image_ref
        local = pack.local_image_path()
        if local is None or not local.is_file():
            return None
        mime, _ = mimetypes.guess_type(local.name)
        try:
            data = local.read_bytes()
        except OSError:
            return None
        return f"data:{mime or 'application/octet-stream'};base64,{base64.b64encode(data).decode('ascii')}"

    def check_conflicts(self, game_version: str) -> dict:
        return modpacks.preview_conflicts(MODPACKS_DIR, LOADORDER_PATH, game_version)

    def find_game_candidates(self) -> list[str]:
        return [str(p) for p in install.find_game_candidates()]

    def pick_game_exe(self) -> str | None:
        """Native file-picker dialog, since a GUI has no terminal to type a
        path into the way install_mod.py's choose_game_exe() expects."""
        if self._window is None:
            return None
        result = self._window.create_file_dialog(FileDialog.OPEN)
        return result[0] if result else None

    def start_install(self, exe_path: str) -> dict:
        with self._install_lock:
            if self._installing:
                return {"ok": False, "error": "an install is already running"}
            self._installing = True

        thread = threading.Thread(
            target=self._run_install, args=(exe_path,), daemon=True)
        thread.start()
        return {"ok": True, "started": True}

    def _run_install(self, exe_path: str) -> None:
        def on_progress(phase: str, message: str) -> None:
            self._emit({"type": "progress", "phase": phase, "message": message})

        try:
            result = install.install(Path(exe_path), on_progress=on_progress)
        except Exception as exc:  # noqa: BLE001 (reported to the UI, not swallowed)
            self._emit({"type": "error", "message": str(exc)})
            with self._install_lock:
                self._installing = False
            return

        launched, launch_error = True, None
        try:
            install.launch(result.output)
        except Exception as exc:  # noqa: BLE001 (the build itself still succeeded)
            launched, launch_error = False, str(exc)

        self._emit({
            "type": "done",
            "output": str(result.output),
            "size_bytes": result.size_bytes,
            "game_version": result.game_version,
            "modpacks": result.modpacks,
            "launched": launched,
            "launch_error": launch_error,
        })
        with self._install_lock:
            self._installing = False

    def export_build(self, built_path: str, destination: str) -> dict:
        try:
            saved = install.export_copy(Path(built_path), Path(destination))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": str(saved)}

    def pick_export_destination(self, default_name: str) -> str | None:
        if self._window is None:
            return None
        result = self._window.create_file_dialog(
            FileDialog.SAVE, save_filename=default_name)
        return result[0] if result else None

    def _emit(self, payload: dict) -> None:
        if self._window is None:
            return
        try:
            self._window.evaluate_js(
                f"window.onInstallEvent && window.onInstallEvent({json.dumps(payload)})")
        except Exception:
            pass  # window may have closed mid-install; nothing left to update


def main() -> None:
    api = Api()
    window = webview.create_window(
        "TCV Modloader",
        url=str(UI_INDEX),
        js_api=api,
        width=900,
        height=640,
        min_size=(700, 500),
    )
    api._window = window
    webview.start(gui="qt")


if __name__ == "__main__":
    main()
