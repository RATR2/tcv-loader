"""The actual build engine: downloading gdRE/Godot, decompiling a copy of the
game, applying patches, and exporting a modded build. loader_core.install
drives this through apply_combined_mod() (modpacks/combiner-based) instead of
this module's own notion of "the mod"; there isn't one here, on purpose.
This is infrastructure a single mod, several combined mods, or a maintainer
script (see dev/tools/) can all sit on top of without re-implementing it.

Used exclusively through loader/app.py (or scripts under dev/tools/);
there is no separate command-line installer any more. Run it with
./run.sh or run.bat, which set up a virtual environment and launch the
desktop app.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

from loader_core import repo_root

REPO_ROOT = repo_root()

SUPPORTED_VERSIONS = ["0.5.1", "0.5.2", "0.5.3"]
GAME_VERSION = " or ".join(SUPPORTED_VERSIONS)

KNOWN_GAME_SIZES = {
    211382192: "0.5.1 compatibility build",
    211382048: "0.5.1 build",
    199462432: "0.5.2 dev-2 compatibility build",
    208463056: "0.5.2 compatibility build",
    208462000: "0.5.2 standard build",
    208741456: "0.5.3 compatibility build",
    208741312: "0.5.3 standard build",
    # Linux (.x86_64) builds: same versions, packaging-only diff from Windows; project.godot and scripts are byte-identical.
    214540944: "0.5.3 standard build (Linux)",
    214541088: "0.5.3 compatibility build (Linux)",
}

GODOT_VERSION = "4.4.1-stable"
GODOT_TEMPLATE_DIR_NAME = "4.4.1.stable"
GODOT_BUILDS_BASE = "https://github.com/godotengine/godot-builds/releases/download/4.4.1-stable"
TEMPLATES_URL = f"{GODOT_BUILDS_BASE}/Godot_v4.4.1-stable_export_templates.tpz"
GDRE_VERSION = "v2.6.3"
GDRE_RELEASES_BASE = "https://github.com/GDRETools/gdsdecomp/releases/download/v2.6.3"

# HOST: the OS this process runs on, picks which gdre_tools/Godot editor binary to fetch
# (those tools must run natively here). TARGET: the OS of the handed-in game exe
# (detect_target_platform()), picks the export template + preset. Usually the same
# machine, but not assumed to be.
HOST = platform.system()  # "Windows", "Linux", or "Darwin"

GODOT_EDITOR_URLS = {
    "Windows": f"{GODOT_BUILDS_BASE}/Godot_v4.4.1-stable_win64.exe.zip",
    "Linux": f"{GODOT_BUILDS_BASE}/Godot_v4.4.1-stable_linux.x86_64.zip",
}
GDRE_URLS = {
    "Windows": f"{GDRE_RELEASES_BASE}/GDRE_tools-v2.6.3-windows.zip",
    "Linux": f"{GDRE_RELEASES_BASE}/GDRE_tools-v2.6.3-linux.zip",
}
GODOT_URL = GODOT_EDITOR_URLS.get(HOST, GODOT_EDITOR_URLS["Windows"])
GDRE_URL = GDRE_URLS.get(HOST, GDRE_URLS["Windows"])

# "template" is the member name inside TEMPLATES_URL's .tpz; linux's dot vs windows's underscore is Godot's own naming, not a typo.
TARGET_PLATFORMS = {
    "Windows Desktop": {
        "preset": "Windows Desktop",
        "template": "templates/windows_release_x86_64.exe",
        "template_filename": "windows_release_x86_64.exe",
        "output_suffix": ".exe",
    },
    "Linux": {
        "preset": "Linux",
        "template": "templates/linux_release.x86_64",
        "template_filename": "linux_release.x86_64",
        "output_suffix": ".x86_64",
    },
}

# Baked into mod/export_presets.cfg's export_path; install.write_export_preset() finds-and-replaces
# it for its own OUTPUT_STEM. Not a real build name; there isn't one without a single mod to name it after.
OUTPUT_STEM = "TheChoicerVoicer-Multiplayer"

ISSUES_URL = "https://github.com/TypeOneAppolo/tcv-multiplayer-mod/issues"

RUN_LOG_DIR = REPO_ROOT / "install_logs"

NODE_PATH_RE = re.compile(r'(\$%?[A-Za-z_]\w*)((?:\s*/\s*%?[A-Za-z_]\w*)+)')


class Failed(Exception):
    pass


def say(step: str, msg: str) -> None:
    print(f"[{step}] {msg}", flush=True)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _https_context() -> ssl.SSLContext | None:
    if os.environ.get("TCV_INSECURE_SSL") == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    return None


def install_https_opener() -> None:
    ctx = _https_context()
    if ctx is not None:
        handler = urllib.request.HTTPSHandler(context=ctx)
        urllib.request.install_opener(urllib.request.build_opener(handler))


def _tls_hint(exc: Exception) -> str:
    if "CERTIFICATE_VERIFY" not in str(exc):
        return ""
    return (
        "\n\n  This is a TLS certificate error, not a problem with the mod.\n"
        "  Your Python can't verify GitHub's certificate. Fixes, easiest first:\n"
        "    1. pip install certifi truststore   then run this again.\n"
        "    2. If your network or antivirus inspects HTTPS, truststore (above)\n"
        "       picks up its root from the Windows store once installed.\n"
        "    3. Last resort, skip verification for this run:\n"
        "         set TCV_INSECURE_SSL=1     (cmd)\n"
        "         $env:TCV_INSECURE_SSL=1    (PowerShell)\n"
        "       then re-run."
    )


def download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        say("cache", f"already have {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    say("get", url.rsplit("/", 1)[-1])
    seen = [0]

    def hook(count: int, block: int, total: int) -> None:
        seen[0] = count * block
        if total > 0:
            pct = min(100, seen[0] * 100 // total)
            print(f"\r      {pct:3d}%  ({seen[0] // 1048576} / {total // 1048576} MB)",
                  end="", flush=True)

    try:
        urllib.request.urlretrieve(url, tmp, hook)
    except Exception as exc:
        raise Failed(f"could not download {url}\n  {exc}{_tls_hint(exc)}")
    print()
    tmp.replace(dest)
    return dest


def http_range(url: str, start: int, length: int) -> bytes:
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{start + length - 1}"})
    with urllib.request.urlopen(req) as resp:
        if resp.status != 206:
            raise Failed("the download server would not serve a partial file")
        return resp.read()


def http_size(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req) as resp:
        return int(resp.headers["Content-Length"])


def download_zip_member(url: str, member: str, dest: Path) -> None:
    import struct
    import zlib

    total = http_size(url)
    tail_len = min(65536 + 22, total)
    tail = http_range(url, total - tail_len, tail_len)
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise Failed("could not read the archive index")
    cd_size, cd_offset = struct.unpack("<II", tail[eocd + 12:eocd + 20])
    if cd_offset == 0xFFFFFFFF:
        raise Failed("zip64 archive, not supported here")

    cd = http_range(url, cd_offset, cd_size)
    pos = 0
    found = None
    while pos < len(cd) - 4 and cd[pos:pos + 4] == b"PK\x01\x02":
        method, = struct.unpack("<H", cd[pos + 10:pos + 12])
        comp_size, uncomp_size = struct.unpack("<II", cd[pos + 20:pos + 28])
        name_len, extra_len, comment_len = struct.unpack("<HHH", cd[pos + 28:pos + 34])
        local_offset, = struct.unpack("<I", cd[pos + 42:pos + 46])
        name = cd[pos + 46:pos + 46 + name_len].decode("utf-8", "replace")
        if name == member:
            found = (method, comp_size, uncomp_size, local_offset)
            break
        pos += 46 + name_len + extra_len + comment_len
    if not found:
        raise Failed(f"{member} is not in the archive")

    method, comp_size, uncomp_size, local_offset = found
    head = http_range(url, local_offset, 30)
    if head[:4] != b"PK\x03\x04":
        raise Failed("archive index points at nothing")
    lname_len, lextra_len = struct.unpack("<HH", head[26:30])
    data_at = local_offset + 30 + lname_len + lextra_len

    say("get", f"{member.rsplit('/', 1)[-1]} ({comp_size // 1048576} MB "
               f"instead of {total // 1048576} MB)")
    blob = http_range(url, data_at, comp_size)
    if method == 0:
        raw = blob
    elif method == 8:
        raw = zlib.decompress(blob, -15)
    else:
        raise Failed(f"unsupported compression method {method}")
    if len(raw) != uncomp_size:
        raise Failed("the extracted file is the wrong size")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)


def unzip(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(dest)


def run(cmd: list[str], what: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    log_subprocess(what, cmd, proc)
    if proc.returncode != 0:
        tail = (proc.stdout or "") + (proc.stderr or "")
        raise Failed(f"{what} failed (exit {proc.returncode})\n"
                     + "\n".join(tail.strip().splitlines()[-25:]))
    return proc


# --- debug logging & diagnostics ------------------------------------------
# Every run gets a full timestamped log, not just the on-screen failure tail; on
# failure it's bundled with the game's own logs into a zip ready to attach to an issue.

CURRENT_RUN_LOG: Path | None = None
_LOG_FH = None


class _Tee:
    """Mirrors writes to several streams at once, e.g. the real console and a
    log file, so nothing printed during a run is only ever seen once."""

    def __init__(self, *streams) -> None:
        self.streams = streams

    def write(self, data: str) -> int:
        for stream in self.streams:
            try:
                stream.write(data)
            except Exception:
                pass
        return len(data)

    def flush(self) -> None:
        for stream in self.streams:
            try:
                stream.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return bool(self.streams) and self.streams[0].isatty()


def game_log_dir() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    path = Path(appdata) / "YeahMaybe" / "ChoicerVoicer" / "logs"
    return path if path.is_dir() else None


def start_run_log() -> None:
    """Best-effort: a machine where this can't be set up still gets to run
    the installer, it just won't have a log to show for it afterwards."""
    global CURRENT_RUN_LOG, _LOG_FH
    try:
        RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = RUN_LOG_DIR / f"install-{stamp}.log"
        fh = open(path, "w", encoding="utf-8", errors="replace")
        fh.write(f"TCV Modloader: log started {stamp}\n")
        fh.write(f"python:       {sys.version.split()[0]}\n")
        fh.write(f"platform:     {platform.platform()}\n")
        fh.write("-" * 70 + "\n\n")
        fh.flush()
    except OSError:
        return

    CURRENT_RUN_LOG = path
    _LOG_FH = fh
    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)

    for stale in sorted(RUN_LOG_DIR.glob("install-*.log"),
                        key=lambda p: p.stat().st_mtime, reverse=True)[10:]:
        try:
            stale.unlink()
        except OSError:
            pass


def log_subprocess(what: str, cmd: list[str], proc: subprocess.CompletedProcess) -> None:
    """The console only ever sees the last few lines of a failed command;
    the full output of everything run, pass or fail, goes straight to the log
    file instead of the screen so it's there later without cluttering now."""
    if not _LOG_FH:
        return
    try:
        _LOG_FH.write(f"\n$ {what}\n  {' '.join(cmd)}\n  exit code: {proc.returncode}\n")
        if proc.stdout:
            _LOG_FH.write("  --- stdout ---\n" + proc.stdout)
            if not proc.stdout.endswith("\n"):
                _LOG_FH.write("\n")
        if proc.stderr:
            _LOG_FH.write("  --- stderr ---\n" + proc.stderr)
            if not proc.stderr.endswith("\n"):
                _LOG_FH.write("\n")
        _LOG_FH.flush()
    except OSError:
        pass


def zip_diagnostics() -> Path | None:
    """Bundles this run's log (plus a few recent ones), the game's own logs,
    and a note on where to send it all into one zip. Returns None if there
    was nothing worth zipping."""
    desktop = Path.home() / "Desktop"
    out_dir = desktop if desktop.is_dir() else REPO_ROOT
    zip_path = out_dir / f"tcv-diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.zip"

    added_anything = False
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if _LOG_FH:
                try:
                    _LOG_FH.flush()
                except OSError:
                    pass
            logs = sorted(RUN_LOG_DIR.glob("install-*.log"),
                          key=lambda p: p.stat().st_mtime, reverse=True)[:5]
            for log in logs:
                zf.write(log, f"install_log/{log.name}")
                added_anything = True

            game_logs = game_log_dir()
            if game_logs:
                for f in game_logs.rglob("*"):
                    if f.is_file():
                        zf.write(f, f"game_logs/{f.relative_to(game_logs)}")
                        added_anything = True

            zf.writestr("README.txt",
                "What's in here\n"
                "---------------\n"
                "install_log/  : what the modloader did and said, most\n"
                "                 recent run(s) first.\n"
                "game_logs/    : the game's own logs, straight from\n"
                "                 %APPDATA%\\YeahMaybe\\ChoicerVoicer\\logs\\, with\n"
                "                 [NET] lines showing what the multiplayer mod did.\n"
                "\n"
                f"Attach this zip to an issue: {ISSUES_URL}\n")
    except OSError:
        return None

    return zip_path if added_anything else None


# --- patching --------------------------------------------------------------

def parse_hunks(patch_text: str) -> list[tuple[int, list[str]]]:
    lines = patch_text.split("\n")
    hunks: list[tuple[int, list[str]]] = []
    i = 0
    while i < len(lines):
        head = re.match(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", lines[i])
        if not head:
            i += 1
            continue
        start = int(head.group(1))
        body: list[str] = []
        i += 1
        while i < len(lines):
            line = lines[i]
            if line.startswith("@@") or line.startswith("--- ") or line.startswith("+++ "):
                break
            if line.startswith(("\\",)):
                i += 1
                continue
            body.append(line)
            i += 1
        while body and body[-1] == "":
            body.pop()
        hunks.append((start, body))
    return hunks


def apply_patch(target: Path, patch_text: str, version: str = "") -> None:
    lines = read_text(target).split("\n")
    offset = 0
    for index, (start, body) in enumerate(parse_hunks(patch_text), 1):
        old: list[str] = []
        new: list[str] = []
        for raw in body:
            tag, content = raw[:1], raw[1:]
            if tag == "-":
                old.append(content)
            elif tag == "+":
                new.append(content)
            else:
                old.append(content)
                new.append(content)

        want = start - 1 + offset
        found = -1
        for delta in range(0, 400):
            for pos in {want + delta, want - delta}:
                if 0 <= pos <= len(lines) - len(old) and lines[pos:pos + len(old)] == old:
                    found = pos
                    break
            if found >= 0:
                break
        if found < 0:
            raise Failed(f"{target.name}: hunk {index} does not match.\n"
                         f"  This build looks like {version or 'an unknown version'}, "
                         "but its scripts are not what the mod expects.\n"
                         "  Either it is a version this mod has not caught up with, "
                         "or the project is already patched.")
        lines[found:found + len(old)] = new
        offset += len(new) - len(old)
    write_text(target, "\n".join(lines))


# --- locating the game -------------------------------------------------

def steam_libraries() -> list[Path]:
    roots: list[Path] = []
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if base:
            roots.append(Path(base) / "Steam")
    home = os.environ.get("LOCALAPPDATA")
    if home:
        roots.append(Path(home) / "Steam")
    # Linux: the usual native install path, plus the older ~/.steam symlink.
    roots.append(Path.home() / ".local" / "share" / "Steam")
    roots.append(Path.home() / ".steam" / "steam")

    libs: list[Path] = []
    for root in roots:
        apps = root / "steamapps"
        if apps.is_dir():
            libs.append(apps / "common")
        vdf = apps / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                for match in re.finditer(r'"path"\s+"([^"]+)"', read_text(vdf)):
                    libs.append(Path(match.group(1).replace("\\\\", "\\")) / "steamapps" / "common")
            except OSError:
                pass
    return [lib for lib in libs if lib.is_dir()]


def detect_target_platform(exe: Path) -> str:
    """Which export target this copy of the game belongs to, from the file
    itself rather than its extension; a Linux build dragged in without its
    original name still has the right magic bytes."""
    try:
        with open(exe, "rb") as fh:
            head = fh.read(4)
    except OSError:
        head = b""
    if head[:4] == b"\x7fELF":
        return "Linux"
    if head[:2] == b"MZ":
        return "Windows Desktop"
    # fall back to the extension if the file couldn't be sniffed
    return "Linux" if exe.suffix.lower() in ("", ".x86_64") else "Windows Desktop"


def find_game_exe() -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()

    def consider(path: Path) -> None:
        key = str(path).lower()
        if key in seen or not path.is_file():
            return
        seen.add(key)
        # anything we built ourselves, whichever version is on the end of it.
        if path.stem.lower().startswith(OUTPUT_STEM.lower()) or path.stem.lower().startswith("thechoicervoicer-modded"):
            return
        candidates.append(path)

    places = list(steam_libraries())
    home = Path.home()
    places += [home / "Downloads", home / "Desktop", home / "Documents", Path.cwd()]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        places.append(Path(local) / "itch" / "apps")
    # itch's Linux and macOS app installs, and a couple of common manual spots.
    places.append(home / ".config" / "itch" / "apps")
    places.append(home / ".itch" / "apps")
    places.append(home / "Games")

    for place in places:
        if not place.is_dir():
            continue
        try:
            for pattern in ("TheChoicerVoicer*.exe", "*/TheChoicerVoicer*.exe",
                            "*/*/TheChoicerVoicer*.exe",
                            "TheChoicerVoicer*.x86_64", "*/TheChoicerVoicer*.x86_64",
                            "*/*/TheChoicerVoicer*.x86_64"):
                for hit in place.glob(pattern):
                    consider(hit)
        except OSError:
            continue

    candidates.sort(key=lambda p: (p.stat().st_size not in KNOWN_GAME_SIZES, str(p).lower()))
    return candidates


def check_game_exe(exe: Path) -> str:
    """Validates the file and returns its detected target platform key into
    TARGET_PLATFORMS ("Windows Desktop" or "Linux")."""
    if not exe.is_file():
        raise Failed(f"no such file: {exe}")
    target = detect_target_platform(exe)
    known = KNOWN_GAME_SIZES.get(exe.stat().st_size)
    if known:
        say("game", f"{exe.name} looks like the {known}")
    else:
        say("warn", f"{exe.name} is not a build this mod has been tested against.\n"
                    f"        Trying anyway as a {target} build. Expect a clear error "
                    f"shortly if it is not {GAME_VERSION}.")
    return target


# --- gdRE / Godot ------------------------------------------------------

def ensure_executable(path: Path) -> Path:
    """zipfile doesn't preserve the Unix executable bit on extraction, so a
    freshly-unzipped gdre_tools.x86_64 or Godot binary comes out world-readable
    but not runnable. Windows has no such bit to worry about."""
    if HOST != "Windows":
        try:
            path.chmod(path.stat().st_mode | 0o111)
        except OSError:
            pass
    return path


def get_gdre(cache: Path, supplied: str | None) -> Path:
    if supplied:
        path = Path(supplied)
        if not path.is_file():
            raise Failed(f"--gdre {path} does not exist")
        return ensure_executable(path)
    archive = download(GDRE_URL, cache / f"gdre-{GDRE_VERSION}-{HOST}.zip")
    out = cache / f"gdre-{GDRE_VERSION}-{HOST}"
    if not out.exists():
        unzip(archive, out)
    exe_name = "gdre_tools.exe" if HOST == "Windows" else "gdre_tools.x86_64"
    for candidate in out.rglob(exe_name):
        return ensure_executable(candidate)
    raise Failed(f"{exe_name} not found inside the downloaded archive")


def get_godot(cache: Path, supplied: str | None) -> Path:
    glob_pattern = "Godot_v*_win64.exe" if HOST == "Windows" else "Godot_v*_linux.x86_64"
    if supplied:
        path = Path(supplied)
        if path.is_dir():
            inner = list(path.glob(glob_pattern))
            if inner:
                return ensure_executable(inner[0])
        if not path.is_file():
            raise Failed(f"--godot {path} does not exist")
        return ensure_executable(path)
    archive = download(GODOT_URL, cache / f"godot-{GODOT_VERSION}-{HOST}.zip")
    out = cache / f"godot-{GODOT_VERSION}-{HOST}"
    if not out.exists():
        unzip(archive, out)
    for candidate in out.rglob(glob_pattern):
        if "console" not in candidate.name:
            return ensure_executable(candidate)
    raise Failed("Godot executable not found inside the downloaded archive")


def templates_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Godot" / "export_templates" / GODOT_TEMPLATE_DIR_NAME
    return (Path.home() / ".local" / "share" / "godot" / "export_templates"
            / GODOT_TEMPLATE_DIR_NAME)


def install_full_templates(cache: Path, dest: Path) -> None:
    archive = download(TEMPLATES_URL, cache / f"godot-templates-{GODOT_VERSION}.tpz")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        for entry in zf.namelist():
            if entry.endswith("/"):
                continue
            name = entry.split("/", 1)[1] if "/" in entry else entry
            with zf.open(entry) as src, open(dest / name, "wb") as dst:
                shutil.copyfileobj(src, dst)


def ensure_templates(cache: Path, target: str) -> None:
    member = TARGET_PLATFORMS[target]["template"]
    dest = templates_dir()
    wanted = dest / TARGET_PLATFORMS[target]["template_filename"]
    if wanted.is_file():
        say("skip", f"export template already installed in {dest}")
        return
    try:
        download_zip_member(TEMPLATES_URL, member, wanted)
    except Exception as exc:
        say("warn", f"could not grab just the one template ({exc}); "
                    "falling back to the full 1.1 GB archive")
        install_full_templates(cache, dest)


# --- decompiling & exporting --------------------------------------------

def decompile(gdre: Path, exe: Path, work: Path) -> None:
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    run([str(gdre), "--headless", f"--recover={exe}", f"--output-dir={work}"],
        "decompiling the game")
    if not (work / "project.godot").is_file():
        raise Failed("decompile produced no project.godot. Is that the game exe?")


def repair_node_paths(work: Path) -> int:
    fixed = 0
    for path in work.rglob("*.gd"):
        text = read_text(path)
        patched = NODE_PATH_RE.sub(
            lambda m: m.group(1) + re.sub(r"\s*/\s*", "/", m.group(2)), text)
        if patched != text:
            write_text(path, patched)
            fixed += 1
    return fixed


def detect_version(work: Path) -> str:
    """Prefer the game's own in-code version constant over project.godot's
    config/version field. The 0.5.3 patch shipped with GAME_VERSION bumped in
    common/globals/m.gd but config/version left at "0.5.2": the field the
    devs actually show players is the reliable one, the project metadata
    isn't."""
    m_gd = work / "common" / "globals" / "m.gd"
    if m_gd.is_file():
        match = re.search(r'const\s+GAME_VERSION\s*:\s*String\s*=\s*"([^"]+)"', read_text(m_gd))
        if match:
            return match.group(1)
    match = re.search(r'config/version="([^"]+)"', read_text(work / "project.godot"))
    if not match:
        raise Failed("project.godot has no config/version; unexpected game build")
    return match.group(1)


def powershell(script: str, timeout: int = 30) -> str:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return ""
    try:
        proc = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, errors="replace",
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip()


def defender_exclusions() -> list[str]:
    raw = powershell("(Get-MpPreference -ErrorAction SilentlyContinue).ExclusionPath -join '|'")
    return [p for p in raw.split("|") if p]


def add_defender_exclusion(folder: Path) -> bool:
    """Asks Windows to elevate (one UAC prompt) and adds the exclusion itself,
    so most people never hit the "the export produced no file" failure at all
    instead of only being told how to fix it after Defender has already deleted
    a build. Best-effort: a declined UAC prompt or a non-admin account just
    means the reactive fallback in explain_missing_export() still applies."""
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return False
    inner = f"Add-MpPreference -ExclusionPath '{folder}'"
    try:
        subprocess.run(
            [exe, "-NoProfile", "-Command",
             f"Start-Process powershell -Verb RunAs -Wait -ArgumentList "
             f"'-NoProfile -Command \"{inner}\"'"],
            capture_output=True, text=True, errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return str(folder) in defender_exclusions()


def offer_defender_exclusion(folder: Path) -> None:
    """CLI-only in practice: a GUI session has no stdin to prompt on, so this
    is a no-op there (see the isatty() check) rather than something that
    needs a separate non-interactive path."""
    if not sys.stdin.isatty() or not realtime_protection_on():
        return
    if str(folder) in defender_exclusions():
        return
    print("\nWindows Defender is on, and it's the single most common reason this")
    print("installer fails: it quarantines the freshly built exe the instant Godot")
    print(f"renames it, in {folder}. I can add a Defender exclusion for that folder")
    print("now (one UAC prompt) and the build should just work. Say no and it")
    print("builds anyway; if Defender does grab it you'll get the same offer again")
    print("after, with the exact detection.")
    try:
        answer = input("\nAdd a Defender exclusion for that folder now? [Y/N] ").strip()
    except EOFError:
        return
    if answer.lower() != "y":
        return
    if add_defender_exclusion(folder):
        say("defender", f"excluded {folder}")
    else:
        say("warn", "could not confirm the exclusion went in, carrying on anyway")


def defender_detections(output: Path) -> list[str]:
    """Recent Defender detections that name our build. Empty if it isn't Defender."""
    raw = powershell(
        "Get-MpThreatDetection -ErrorAction SilentlyContinue | "
        "Sort-Object InitialDetectionTime | Select-Object -Last 12 | ForEach-Object { "
        "\"$($_.InitialDetectionTime)  $($_.ThreatName)  $($_.Resources -join ' ')\" }")
    stem = output.stem.lower()
    return [line.strip() for line in raw.splitlines() if stem in line.lower()]


def realtime_protection_on() -> bool:
    answer = powershell("(Get-MpComputerStatus -ErrorAction SilentlyContinue)"
                        ".RealTimeProtectionEnabled")
    return answer.strip().lower() == "true"


def recover_leftover_build(output: Path, work: Path) -> bool:
    """Godot builds to <name>.tmp and renames it at the very end. If that rename
    lost a race, the whole build is sitting right there under the wrong name."""
    for stray in (output.with_suffix(".tmp"), work / (output.stem + ".tmp")):
        if stray.is_file() and stray.stat().st_size > 1048576:
            stray.replace(output)
            say("build", f"the exporter left the build as {stray.name}; renamed it")
            return True
    return False


def explain_missing_export(proc: subprocess.CompletedProcess, output: Path) -> None:
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
    interesting = [line for line in tail
                   if "ERROR" in line or "error" in line or "Failed" in line]
    if interesting:
        print("\nGodot said:")
        for line in interesting[-8:]:
            print(f"  {line}")

    print(f"\nGodot finished without complaining, but {output.name} is not there.")

    hits = defender_detections(output)
    if hits:
        print("\nWindows Defender deleted it. Its own log says so:")
        for line in hits[-3:]:
            print(f"  {line}")
    elif realtime_protection_on():
        print("\nAlmost always this is antivirus. A freshly built, unsigned Godot game")
        print("looks exactly like the thing malware scanners are trained to catch, and")
        print("Defender quarantines it the moment the file is renamed to .exe.")
    else:
        print("\nUsually this is antivirus quarantining the new .exe the instant it")
        print("appears. Check whatever scanner you run for a blocked item.")

    folder = output.resolve().parent
    print("\nTo let it through, open PowerShell as administrator and run:")
    print(f'  Add-MpPreference -ExclusionPath "{folder}"')
    print("\nIf Defender already took a copy, release it too:")
    print("  Start-Process ms-settings:windowsdefender")
    print("  (Virus & threat protection -> Protection history -> Allow)")
    print("\nThe exclusion only covers that one folder, and you can drop it again")
    print(f'afterwards with Remove-MpPreference -ExclusionPath "{folder}".')


def export(godot: Path, work: Path, output: Path, target: str) -> None:
    say("build", "importing project assets (this takes a minute)")
    import_cmd = [str(godot), "--headless", "--path", str(work), "--import"]
    probe = subprocess.run(import_cmd, capture_output=True, text=True, errors="replace")
    log_subprocess("checking whether the project needs a one-time editor import",
                   import_cmd, probe)
    if probe.returncode != 0:
        run([str(godot), "--headless", "--path", str(work), "--editor", "--quit"],
            "importing the project")

    preset = TARGET_PLATFORMS[target]["preset"]
    output.parent.mkdir(parents=True, exist_ok=True)
    while True:
        say("build", f"exporting to {output}")
        proc = run([str(godot), "--headless", "--path", str(work),
                    "--export-release", preset, str(output.resolve())],
                   "exporting the game")
        if output.is_file() or recover_leftover_build(output, work):
            return
        explain_missing_export(proc, output)
        if not sys.stdin.isatty():
            raise Failed("the export produced no file, see the notes above")
        print()
        if input("Press Enter to build again once that's done, or type q to give up: "
                 ).strip().lower().startswith("q"):
            raise Failed("the export produced no file, see the notes above")


def report_failure() -> Path | None:
    """A message on its own means someone has to copy-paste the error and
    then get asked "what were you running it against" anyway. Handing back a
    ready-made zip skips that whole round trip."""
    try:
        return zip_diagnostics()
    except Exception:
        return None
