#!/usr/bin/env python3
"""
Image Upscaler - a cross-platform (Windows / macOS / Linux) tool that enlarges
photos and drawings 2× or 4× with AI (Real-ESRGAN), in batches, on your own
computer.

It runs the official Real-ESRGAN ncnn-vulkan program, which uses the graphics
chip (any with Vulkan support: NVIDIA, AMD, Intel, Apple Silicon). The
downloadable app includes it; running from source, the app offers to download
it once from the Real-ESRGAN releases on GitHub.

Requirements:
    Python 3.8+ (with Tkinter, included in the python.org installers)
    Pillow, pillow-heif -> read and write images (pillow-heif opens iPhone HEIC photos)
    certifi            -> trusted certificates for downloading the upscaler
    tkinterdnd2        -> drag and drop (optional: the app works without it)
    The app offers to install them on first run. Manual install, using the Python that runs the app:
        python -m pip install pillow pillow-heif certifi tkinterdnd2

Run:
    python image_upscaler.py
"""

import hashlib
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# Filled in by load_libraries(), so the window can still open (and offer to
# install them) when they're missing.
Image = ImageOps = None

PACKAGES = ["pillow", "pillow-heif", "certifi", "tkinterdnd2"]
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}
FILE_TYPES = [
    ("Images", " ".join("*" + e for e in sorted(IMAGE_EXTS))),
    ("All files", "*.*"),
]

MODELS = {
    "Photos — RealESRGAN x4plus": dict(
        name="realesrgan-x4plus",
        about="For photos and real-world pictures: restores detail and removes JPEG blockiness."),
    "Anime & drawings — RealESRGAN x4plus anime": dict(
        name="realesrgan-x4plus-anime",
        about="For anime, cartoons, illustrations and other drawn art: clean lines and flat colors. "
              "2–3× faster than the photo model."),
}
SCALES = {"4× (the model's own size)": 4, "2× (made from the 4× result)": 2}
FORMATS = {"PNG (lossless)": "png", "JPG": "jpg", "WebP": "webp"}
JPG_QUALITY = 92
WEBP_QUALITY = 90
WEBP_MAX_SIDE = 16383
# The model always works at 4×. Bigger than this (e.g. a 12-megapixel photo
# would become 195 MP) takes minutes, gigabytes of memory, and huge files.
MAX_UPSCALED_PIXELS = 150_000_000

# The official Real-ESRGAN ncnn-vulkan release: program + models, one zip per system.
ENGINE_VERSION = "20220424"
ENGINE_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/"
ENGINE_ZIPS = {  # platform -> (file, sha256, size)
    "win32": ("realesrgan-ncnn-vulkan-20220424-windows.zip",
              "abc02804e17982a3be33675e4d471e91ea374e65b70167abc09e31acb412802d", 45474481),
    "darwin": ("realesrgan-ncnn-vulkan-20220424-macos.zip",
               "e0ad05580abfeb25f8d8fb55aaf7bedf552c375b5b4d9bd3c8d59764d2cc333a", 51817124),
    "linux": ("realesrgan-ncnn-vulkan-20220424-ubuntu.zip",
              "e5aa6eb131234b87c0c51f82b89390f5e3e642b7b70f2b9bbe95b6a285a40c96", 46931474),
}
ENGINE_EXE = "realesrgan-ncnn-vulkan.exe" if sys.platform == "win32" else "realesrgan-ncnn-vulkan"


def load_libraries():
    """Import Pillow and the HEIC plugin. Returns the pip names of missing
    packages (tkinterdnd2 is optional and checked separately)."""
    global Image, ImageOps
    import importlib
    importlib.invalidate_caches()  # see packages installed since startup
    missing = []
    try:
        from PIL import Image, ImageOps
        Image.MAX_IMAGE_PIXELS = 250_000_000  # 4× results are big; refuse absurd ("bomb") files
    except ImportError:
        missing.append("pillow")
    try:
        import pillow_heif
        if Image:
            pillow_heif.register_heif_opener()
    except ImportError:
        missing.append("pillow-heif")
    try:
        import certifi  # noqa: F401
    except ImportError:
        missing.append("certifi")
    try:
        import tkinterdnd2  # noqa: F401
    except ImportError:
        missing.append("tkinterdnd2")
    return missing


# Hide the console window that would otherwise flash up on Windows.
_POPEN_FLAGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def install_requirements(packages):
    """pip-install the packages into *this* Python interpreter.

    Using sys.executable guarantees they land in the same Python that's running
    the app — the usual cause of 'I installed it but it's not found' is having
    several Pythons, where `pip` and double-clicking the script use different ones.
    """
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):  # pythonw has no console; use python.exe for pip
        exe = exe[:-len("pythonw.exe")] + "python.exe"
    r = subprocess.run([exe, "-m", "pip", "install", "--upgrade"] + packages,
                       capture_output=True, text=True, errors="replace", **_POPEN_FLAGS)
    add_user_site()
    return r.returncode == 0, (r.stderr or r.stdout).strip().splitlines()[-3:]


def add_user_site():
    """Make packages pip just put in the per-user folder importable right away.

    When Python's own folder isn't writable (e.g. installed under Program
    Files), pip installs to the user folder instead (%APPDATA%\\Python\\...
    on Windows, ~/Library/Python/... on macOS). Python only adds that folder
    to sys.path at startup if it already existed, so on a first install it
    isn't searched until the app is restarted.
    """
    import site
    path = site.getusersitepackages() if site.ENABLE_USER_SITE else None  # off in venvs
    if path and os.path.isdir(path) and path not in sys.path:
        site.addsitedir(path)


# --------------------------------------------------------------------------- #
#  The Real-ESRGAN program: finding it, downloading it, running it
# --------------------------------------------------------------------------- #
def engine_dirs():
    """Folders the program may be in, in order: inside the downloadable app,
    next to this script, or where the app downloads it."""
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)), "engine"))
    if os.getenv("IMAGE_UPSCALER_ENGINE"):
        dirs.append(os.getenv("IMAGE_UPSCALER_ENGINE"))
    dirs.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine"))
    dirs.append(download_dir())
    return dirs


def download_dir():
    return os.path.join(os.path.expanduser("~"), ".image-upscaler", f"realesrgan-ncnn-vulkan-{ENGINE_VERSION}")


def find_engine():
    """Path to the folder holding the program and its models/, or None."""
    for d in engine_dirs():
        if os.path.isfile(os.path.join(d, ENGINE_EXE)) and os.path.isdir(os.path.join(d, "models")):
            return d
    return None


def download_engine(on_progress, check_cancel):
    """Download the official zip for this system, check its SHA-256, and unpack it."""
    import ssl
    import urllib.request
    import zipfile
    name, sha, size = ENGINE_ZIPS.get(sys.platform if sys.platform in ENGINE_ZIPS else "linux")
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    dest = download_dir()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".zip.part"
    h = hashlib.sha256()
    req = urllib.request.Request(ENGINE_URL + name, headers={"User-Agent": "Image-Upscaler"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or size)
            done = 0
            while True:
                check_cancel()
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                on_progress(done, total)
        if h.hexdigest() != sha:
            raise DownloadError("The downloaded file is damaged (its checksum doesn't match). Try again.")
        tmp = dest + ".unpacking"
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(part) as z:
            z.extractall(tmp)
        exe = os.path.join(tmp, ENGINE_EXE)
        os.chmod(exe, 0o755)  # zip files don't keep the "executable" permission on Mac/Linux
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(part):
            os.remove(part)


def run_engine(engine, src, dst, model, on_progress, on_proc):
    """Upscale src (a PNG) 4× into dst (a PNG) with the Real-ESRGAN program.
    Returns its log text; raises EngineError if it fails."""
    cmd = [os.path.join(engine, ENGINE_EXE), "-i", src, "-o", dst, "-n", model, "-s", "4",
           "-m", os.path.join(engine, "models"), "-f", "png"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, cwd=engine, **_POPEN_FLAGS)
    on_proc(proc)
    log, buf = [], b""
    # Progress comes as "12.34%" lines ended by \r or \n.
    while True:
        chunk = proc.stderr.read(64)
        if not chunk:
            break
        buf += chunk
        *lines, buf = re.split(rb"[\r\n]", buf)
        for line in lines:
            text = line.decode("utf-8", "replace").strip()
            m = re.fullmatch(r"(\d+(?:\.\d+)?)%", text)
            if m:
                on_progress(float(m[1]) / 100)
            elif text:
                log.append(text)
    proc.wait()
    text = "\n".join(log)
    if proc.returncode != 0 or not os.path.exists(dst):
        raise EngineError(explain_engine_error(text, proc.returncode))
    return text


def explain_engine_error(log, code):
    low = log.lower()
    if "vkcreateinstance" in low or "invalid gpu" in low or "vulkan" in low and "fail" in low or code == 3221225477:
        return ("No compatible graphics chip found (it needs Vulkan). Updating the graphics driver "
                "usually fixes this.")
    if "out of memory" in low or "vkallocatememory" in low:
        return "The graphics chip ran out of memory. Close other programs, or try a smaller image."
    lines = [line for line in log.splitlines() if line.strip()]
    return (lines[-1] if lines else f"The upscaler stopped (code {code}).")


def gpu_name(log):
    """The graphics chip named in the program's log, e.g. "NVIDIA GeForce RTX 3060"."""
    m = re.search(r"\[0 ([^\]]+)\]", log)
    return m[1] if m else None


# --------------------------------------------------------------------------- #
#  Per-image work (worker thread; no Tk calls in here)
# --------------------------------------------------------------------------- #
def probe_image(path):
    """Return a dict (width, height, size, error) for one image, from its header."""
    info = dict(width=None, height=None, size=None, error=None)
    try:
        info["size"] = os.path.getsize(path)
        with Image.open(path) as im:
            w, h = im.size
            if im.getexif().get(0x0112) in (5, 6, 7, 8):  # rotated 90°: shown the other way round
                w, h = h, w
            info.update(width=w, height=h)
        if w * h * 16 > MAX_UPSCALED_PIXELS:
            info["error"] = (f"Too large to upscale: 4× would be {w * 4}×{h * 4}. "
                             "Upscaling is for small images.")
    except Exception as e:
        info["error"] = explain_error(e)
    return info


def prepare_input(path, tmp):
    """Write the image as a plain PNG the program can read, turned the right way
    up (it ignores the rotation tag). Returns (png path, has transparency, color profile)."""
    with Image.open(path) as im:
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        im.load()
        im = ImageOps.exif_transpose(im)
    icc = im.info.get("icc_profile") if im.mode not in ("CMYK", "L", "LA", "I;16", "I", "F") else None
    if im.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        div = 256 if im.mode.startswith("I;16") else 1
        im = im.convert("I").point(lambda v: v / div).convert("L")  # 16-bit gray: scale, don't clip
    alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
    im = im.convert("RGBA" if alpha else "RGB")
    if alpha and im.getchannel("A").getextrema()[0] == 255:
        im, alpha = im.convert("RGB"), False  # an alpha channel with nothing see-through
    out = os.path.join(tmp, "in.png")
    im.save(out, compress_level=1)
    return out, alpha, icc


def finish_output(path, scale, fmt, icc):
    """The upscaled image as file bytes: shrunk to 2× if asked, in the chosen format."""
    import io
    with Image.open(path) as im:
        im.load()
    if scale == 2:
        im = im.resize((im.width // 2, im.height // 2), Image.Resampling.LANCZOS)
    if fmt == "webp" and max(im.size) > WEBP_MAX_SIDE:
        raise EngineError(f"{im.width}×{im.height} is too big for WebP (16383 pixels at most). "
                          "Save as PNG or JPG instead.")
    if fmt == "jpg" and im.mode == "RGBA":
        bg = Image.new("RGB", im.size, "white")  # JPG can't be see-through
        bg.paste(im, mask=im.getchannel("A"))
        im = bg
    buf = io.BytesIO()
    kw = {"icc_profile": icc} if icc else {}
    if fmt == "jpg":
        im.convert("RGB").save(buf, "JPEG", quality=JPG_QUALITY, optimize=True, **kw)
    elif fmt == "webp":
        im.save(buf, "WEBP", quality=WEBP_QUALITY, method=4, **kw)
    else:
        im.save(buf, "PNG", **kw)
    return buf.getvalue(), im.size


def explain_error(e):
    """Turn an exception into a short reason."""
    name = type(e).__name__
    if name == "UnidentifiedImageError":
        return "Not an image, or the file is damaged."
    if name == "DecompressionBombError":
        return "Image is too large."
    if isinstance(e, PermissionError):
        return "Permission denied — the file may be open in another program."
    if isinstance(e, FileNotFoundError):
        return "File not found."
    if isinstance(e, MemoryError):
        return "Not enough memory for an image this large."
    text = str(e)
    if "truncated" in text or "broken" in text.lower():
        return "The file is damaged or incomplete."
    return text or name


def fmt_size(n):
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{max(1, round(n / 1e3))} KB"


class Cancelled(Exception):
    pass


class DownloadError(Exception):
    pass


class EngineError(Exception):
    pass


def enable_drag_and_drop(root, on_drop, on_enter=None, on_leave=None):
    """Accept files dragged onto the window, using tkinterdnd2 if it's installed.
    Returns False when it isn't (or its native part can't load): the Add
    buttons still work, so drag and drop is optional.

    on_drop gets the list of dropped paths. Every widget is registered, so a
    drop works anywhere in the window.
    """
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        TkinterDnD.require(root)
    except Exception:
        return False

    def drop(event):
        # The paths come as a Tcl list: names with spaces are in {braces}.
        on_drop(list(root.tk.splitlist(event.data)))
        return event.action  # "copy": the files stay where they are

    def hook(fn):
        def handler(event):
            if fn:
                fn()
            return event.action
        return handler

    widgets, todo = [], list(root.winfo_children())
    while todo:
        w = todo.pop()
        widgets.append(w)
        todo += w.winfo_children()
    for w in widgets:
        w.drop_target_register(DND_FILES)
        w.dnd_bind("<<Drop>>", drop)
        w.dnd_bind("<<DropEnter>>", hook(on_enter))
        w.dnd_bind("<<DropLeave>>", hook(on_leave))
    return True


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #
class UpscalerApp:
    def __init__(self, root):
        self.root = root
        root.title("Image Upscaler")
        root.minsize(700, 0)
        self.missing = load_libraries()
        self.engine = find_engine()
        self.gpu = None
        self.proc = None
        self.cancelled = False
        self.running = False
        self.items = {}  # tree row id -> dict(path, info)
        self.ui_queue = queue.Queue()
        self.probe_queue = queue.Queue()  # files waiting to be read, one background reader
        self._pump_ui()
        threading.Thread(target=self._probe_worker, daemon=True).start()
        # Show unexpected errors in a dialog. By default Tk prints them to a
        # console, which is invisible when the script is double-clicked.
        root.report_callback_exception = self._report_error
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        home = os.path.expanduser("~")
        pictures = os.path.join(home, "Pictures")
        self.v = {
            "model": tk.StringVar(value=next(iter(MODELS))),
            "scale": tk.StringVar(value=next(iter(SCALES))),
            "fmt": tk.StringVar(value=next(iter(FORMATS))),
            "where": tk.StringVar(value="same"),
            "out_dir": tk.StringVar(value=pictures if os.path.isdir(pictures) else home),
            "reveal": tk.BooleanVar(value=True),
        }
        self._build_ui()
        for name, fn in (("model", self._on_model), ("scale", self._on_scale), ("where", self._on_where)):
            self.v[name].trace_add("write", lambda *_, fn=fn: fn())
            fn()
        self.dnd = enable_drag_and_drop(root, self._on_drop, self._on_drag_enter, self._update_list_label)
        self._update_list_label()

        if self._required_missing():
            self.status.config(text="Some components are missing.")
            # Defer so the main window is drawn before the dialog appears.
            self.root.after(200, self.offer_install)
        elif self.engine:
            self._check_gpu()
        else:
            self.status.config(text="Ready. The upscaler program downloads the first time you use it "
                                    f"({ENGINE_ZIPS.get(sys.platform, ENGINE_ZIPS['linux'])[2] / 1e6:.0f} MB).")

    def _required_missing(self):
        return [m for m in self.missing if m != "tkinterdnd2"]

    def _check_gpu(self):
        """Run the program once on a tiny picture, to name the graphics chip it
        will use (or find out early that there isn't a usable one)."""
        self.status.config(text="Checking the graphics chip…")

        def work():
            tmp = tempfile.mkdtemp(prefix="upscaler-")
            try:
                Image.new("RGB", (16, 16), "gray").save(os.path.join(tmp, "in.png"))
                log = run_engine(self.engine, os.path.join(tmp, "in.png"), os.path.join(tmp, "out.png"),
                                 "realesrgan-x4plus-anime", lambda f: None, lambda p: None)
                name = gpu_name(log)
                self.ui(lambda: (setattr(self, "gpu", name or "your graphics chip"),
                                 self.status.config(text=f"Ready — using {name or 'your graphics chip'}.")))
            except Exception as e:
                log_error(f"GPU check: {type(e).__name__}: {e}")
                self.ui(lambda e=e: self.status.config(
                    text=f"The upscaler can't run on this computer: {explain_error(e)}", foreground=ERROR))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        threading.Thread(target=work, daemon=True).start()

    # ---- dependency installer ------------------------------------------ #
    def offer_install(self):
        if getattr(sys, "frozen", False):
            # The downloadable app bundles everything, and sys.executable is the
            # app itself, so "pip install" would just launch another copy of it.
            messagebox.showerror("Missing components", "Missing: " + ", ".join(self.missing) +
                                 "\n\nThis copy of Image Upscaler is incomplete. Please download it again.")
            return
        names = ", ".join(self.missing)
        if not messagebox.askyesno(
                "Install components?",
                f"Image Upscaler needs: {names}\n\n"
                "Install now? (about 15 MB download)\n\n"
                f"This runs pip for the Python running this app:\n{sys.executable}"):
            self.status.config(text="Components missing — nothing can be done until they're installed.")
            return
        self._set_busy(True)
        self.cancel_btn.config(state="disabled")
        self.status.config(text="Installing… this can take a minute.")
        packages = list(self.missing)

        def work():
            try:
                ok, tail = install_requirements(packages)
            except Exception as e:
                ok, tail = False, [str(e)]
            self.ui(lambda: self._install_done(ok, tail))
        threading.Thread(target=work, daemon=True).start()

    def _install_done(self, ok, tail):
        self._set_busy(False)
        self.missing = load_libraries()
        if ok and not self._required_missing():
            if not self.dnd:
                self.dnd = enable_drag_and_drop(self.root, self._on_drop, self._on_drag_enter,
                                                self._update_list_label)
            self._update_list_label()
            self.status.config(text="Installed! Ready.")
            if self.engine:
                self._check_gpu()
            for iid, item in self.items.items():  # re-read files added before Pillow existed
                item["info"] = None
                self._set_row(iid, status="Reading…")
                self.probe_queue.put((iid, item["path"]))
        else:
            self.status.config(text="Install failed — see the error message.")
            messagebox.showerror(
                "Install failed",
                "Couldn't install automatically:\n\n" + "\n".join(tail) +
                "\n\nTry running this in a terminal:\n"
                f'"{sys.executable}" -m pip install ' + " ".join(PACKAGES))

    # ---- layout --------------------------------------------------------- #
    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}
        app_header(self.root, "Image Upscaler", "Enlarge photos 2× or 4× with AI on your own PC · free, private, no limits", "4×").pack(fill="x")
        main = ttk.Frame(self.root, padding=(12, 10))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)

        f = ttk.LabelFrame(main, text="1. Images", padding=8)
        f.grid(row=0, column=0, sticky="nsew", **pad)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        btns = ttk.Frame(f)
        btns.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.edit_btns = [
            ttk.Button(btns, text="Add files…", command=self.add_files),
            ttk.Button(btns, text="Add folder…", command=self.add_folder),
            ttk.Button(btns, text="Remove", command=self.remove_selected),
            ttk.Button(btns, text="Clear", command=self.clear),
        ]
        for i, b in enumerate(self.edit_btns):
            b.grid(row=0, column=i, padx=(0, 6))
        cols = ("dims", "out", "status")
        self.tree = ttk.Treeview(f, columns=cols, height=8, selectmode="extended")
        for col, text, width, anchor in (("#0", "File", 220, "w"), ("dims", "Pixels", 90, "e"),
                                         ("out", "Becomes", 100, "e"), ("status", "Status", 260, "w")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=col in ("#0", "status"))
        self.tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(f, orient="vertical", command=self.tree.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.list_lbl = ttk.Label(f, text="", foreground=MUTED)
        self.list_lbl.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        f = ttk.LabelFrame(main, text="2. Upscale", padding=8)
        f.grid(row=1, column=0, sticky="ew", **pad)
        ttk.Label(f, text="Model").grid(row=0, column=0, sticky="w", **pad)
        ttk.Combobox(f, values=list(MODELS), textvariable=self.v["model"], state="readonly",
                     width=42).grid(row=0, column=1, columnspan=3, sticky="w", **pad)
        self.model_lbl = ttk.Label(f, text="", foreground=MUTED, wraplength=620)
        self.model_lbl.grid(row=1, column=0, columnspan=4, sticky="w", **pad)
        ttk.Label(f, text="Size").grid(row=2, column=0, sticky="w", **pad)
        ttk.Combobox(f, values=list(SCALES), textvariable=self.v["scale"], state="readonly",
                     width=28).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(f, text="Save as").grid(row=2, column=2, sticky="e", **pad)
        ttk.Combobox(f, values=list(FORMATS), textvariable=self.v["fmt"], state="readonly",
                     width=14).grid(row=2, column=3, sticky="w", **pad)

        f = ttk.LabelFrame(main, text="3. Save to", padding=8)
        f.grid(row=2, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        ttk.Radiobutton(f, text="Same folder as each image", value="same",
                        variable=self.v["where"]).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(f, text="This folder:", value="folder",
                        variable=self.v["where"]).grid(row=1, column=0, sticky="w")
        self.dir_e = ttk.Entry(f, textvariable=self.v["out_dir"])
        self.dir_e.grid(row=1, column=1, sticky="ew", padx=6)
        self.dir_btn = ttk.Button(f, text="Choose…", command=self.pick_folder)
        self.dir_btn.grid(row=1, column=2)
        ttk.Checkbutton(f, text="Show the results in their folder when done",
                        variable=self.v["reveal"]).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        f = ttk.Frame(main)
        f.grid(row=3, column=0, sticky="ew", **pad)
        f.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(f, mode="determinate", maximum=1000)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.cancel_btn = ttk.Button(f, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=1)
        self.go_btn = PillButton(f, "Upscale", self.start)
        self.go_btn.grid(row=0, column=2, padx=(6, 0))
        foot = ttk.Frame(main)  # the status line, with the support link on its right
        foot.grid(row=4, column=0, sticky="ew", **pad)
        foot.columnconfigure(0, weight=1)
        self.status = ttk.Label(foot, text="Ready.", wraplength=510)
        self.status.grid(row=0, column=0, sticky="w")
        support_link(foot).grid(row=0, column=1, sticky="ne", padx=(12, 0))

    # ---- small helpers -------------------------------------------------- #
    def _scale(self):
        return SCALES[self.v["scale"].get()]

    def _on_model(self):
        self.model_lbl.config(text=MODELS[self.v["model"].get()]["about"])

    def _on_scale(self):
        k = self._scale()
        for iid, item in self.items.items():
            i = item["info"]
            if i and not i["error"]:
                self._set_row(iid, out=f"{i['width'] * k}×{i['height'] * k}")

    def _on_where(self):
        state = "normal" if self.v["where"].get() == "folder" else "disabled"
        self.dir_e.config(state=state)
        self.dir_btn.config(state=state)

    def pick_folder(self):
        d = filedialog.askdirectory(title="Choose output folder", initialdir=self.v["out_dir"].get())
        if d:
            self.v["out_dir"].set(d)

    def ui(self, fn):
        """Run fn on the main (UI) thread. Safe to call from any thread.

        Tkinter isn't thread-safe: touching widgets, or even calling
        root.after(), from a worker thread works on some systems and silently
        does nothing on others. So workers only put callbacks in a queue, and
        the UI thread drains it every 50 ms in _pump_ui().
        """
        self.ui_queue.put(fn)

    def _pump_ui(self):
        # Reschedule FIRST: if a queued callback raises, the pump must keep
        # running, or every later message (status, progress, errors) is lost.
        self.root.after(50, self._pump_ui)
        while True:
            try:
                fn = self.ui_queue.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                self._report_error(*sys.exc_info())

    # ---- the file list -------------------------------------------------- #
    def add_files(self):
        self._add_paths(filedialog.askopenfilenames(title="Choose images", filetypes=FILE_TYPES))

    def add_folder(self):
        d = filedialog.askdirectory(title="Choose a folder of images")
        if d:
            self._on_drop([d])

    def _folder_images(self, d):
        return sorted(os.path.join(d, n) for n in os.listdir(d)
                      if os.path.splitext(n)[1].lower() in IMAGE_EXTS
                      and os.path.isfile(os.path.join(d, n)))

    def _on_drag_enter(self):
        if not self.running:
            self.list_lbl.config(text="Drop to add these images.", foreground="")

    def _on_drop(self, paths):
        """Files or folders dropped or chosen (a folder adds the images directly inside it)."""
        self._update_list_label()
        if self.running:
            self.status.config(text="Wait for this batch to finish before adding more images.")
            return
        found, skipped, empty = [], 0, []
        for p in paths:
            if os.path.isdir(p):
                imgs = self._folder_images(p)
                found += imgs
                if not imgs:
                    empty.append(os.path.basename(p) or p)
            elif os.path.splitext(p)[1].lower() in IMAGE_EXTS:
                found.append(p)
            else:
                skipped += 1
        self._add_paths(found)
        notes = []
        if skipped:
            notes.append(f"{skipped} file{'s' * (skipped != 1)} skipped (not JPG, PNG, WebP, HEIC, BMP or TIFF)")
        if empty:
            notes.append("no images directly inside " + ", ".join(empty) + " (subfolders aren't searched)")
        if notes:
            text = "; ".join(notes)
            self.status.config(text=text[0].upper() + text[1:] + ".")

    def _add_paths(self, paths):
        known = {os.path.normcase(os.path.abspath(i["path"])) for i in self.items.values()}
        for p in paths:
            key = os.path.normcase(os.path.abspath(p))
            if key in known:
                continue  # already in the list
            known.add(key)
            iid = self.tree.insert("", "end", text=os.path.basename(p),
                                   values=("", "", "Reading…" if Image else "Needs Pillow"))
            self.items[iid] = dict(path=p, info=None)
            if Image:
                self.probe_queue.put((iid, p))
        self._update_list_label()

    def _probe_worker(self):
        """Background thread: reads each added image's details, one at a time."""
        while True:
            iid, path = self.probe_queue.get()
            info = probe_image(path)
            self.ui(lambda i=iid, inf=info: self._probed(i, inf))

    def _probed(self, iid, info):
        if iid not in self.items:  # removed while it was being read
            return
        self.items[iid]["info"] = info
        if info["width"]:
            self._set_row(iid, dims=f"{info['width']}×{info['height']}")
        if info["error"]:
            self._set_row(iid, status=info["error"])
        else:
            k = self._scale()
            self._set_row(iid, out=f"{info['width'] * k}×{info['height'] * k}", status="Ready")
        self._update_list_label()

    def _set_row(self, iid, **cols):
        if self.tree.exists(iid):
            for col, val in cols.items():
                self.tree.set(iid, col, val)

    def remove_selected(self):
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.items.pop(iid, None)
        self._update_list_label()

    def clear(self):
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self._update_list_label()

    def _update_list_label(self):
        n = len(self.items)
        if not n:
            text = ("Drag images or folders here, or use Add files." if getattr(self, "dnd", False)
                    else "Add images, or a folder (the images directly inside it).")
        else:
            ok = sum(1 for i in self.items.values() if i["info"] and not i["info"]["error"])
            text = f"{n} image{'s' * (n != 1)}"
            if ok < n and all(i["info"] for i in self.items.values()):
                text += f", {n - ok} can't be upscaled (see Status)"
            text += "."
        self.list_lbl.config(text=text, foreground=MUTED)

    # ---- running -------------------------------------------------------- #
    def _plan_outputs(self, todo, scale, ext):
        """Each image's output path. Never the image itself, and never two images
        writing the same file (e.g. art.jpg and art.png)."""
        taken, plans = set(), []
        for iid, item in todo:
            src = item["path"]
            folder = (os.path.dirname(src) if self.v["where"].get() == "same"
                      else self.v["out_dir"].get().strip())
            stem = os.path.splitext(os.path.basename(src))[0]
            out = os.path.join(folder, f"{stem} (upscaled {scale}x).{ext}")
            base, n = os.path.splitext(out)[0], 2
            while os.path.normcase(os.path.abspath(out)) in taken:
                out = f"{base} ({n}).{ext}"
                n += 1
            taken.add(os.path.normcase(os.path.abspath(out)))
            plans.append((iid, out))
        return plans

    def start(self):
        if self._required_missing():
            self.offer_install()
            return
        if not self.items:
            messagebox.showwarning("No images", "Add some images first.")
            return
        if any(i["info"] is None for i in self.items.values()):
            messagebox.showinfo("Still reading images", "Still reading the images' details. "
                                "Try again in a moment.")
            return
        if self.v["where"].get() == "folder" and not os.path.isdir(self.v["out_dir"].get().strip()):
            messagebox.showwarning("Check settings", "The output folder doesn't exist.")
            return
        todo = [(iid, it) for iid, it in self.items.items() if not it["info"]["error"]]
        if not todo:
            messagebox.showwarning("Nothing to do", "None of these images can be upscaled (see Status).")
            return
        if not self.engine:
            name, _, size = ENGINE_ZIPS.get(sys.platform, ENGINE_ZIPS["linux"])
            if not messagebox.askyesno(
                    "Download the upscaler?",
                    f"Image Upscaler uses the official Real-ESRGAN program, a one-time download of "
                    f"{size / 1e6:.0f} MB from the Real-ESRGAN releases on GitHub.\n\n"
                    "After that it works offline. Your images are never uploaded.\n\nDownload now?"):
                return
        s = dict(model=MODELS[self.v["model"].get()]["name"], scale=self._scale(),
                 fmt=FORMATS[self.v["fmt"].get()])
        plans = self._plan_outputs(todo, s["scale"], s["fmt"])
        existing = [out for _, out in plans if os.path.exists(out)]
        skip = set()
        if existing:
            names = "\n".join(os.path.basename(p) for p in existing[:5])
            more = f"\n…and {len(existing) - 5} more" if len(existing) > 5 else ""
            answer = messagebox.askyesnocancel(
                "Files already exist",
                f"{len(existing)} of the output files already exist:\n\n{names}{more}\n\n"
                "Replace them?\n\nYes = replace   No = skip those images   Cancel = stop")
            if answer is None:
                return
            if answer is False:
                skip = set(existing)
        jobs = [(iid, self.items[iid]["path"], out) for iid, out in plans if out not in skip]
        for iid, out in plans:
            self._set_row(iid, status="Skipped (exists)" if out in skip else "Waiting")
        if not jobs:
            self.status.config(text="Nothing to do: every output file already exists.")
            return
        self.cancelled = False
        self._set_busy(True)
        # The program runs off the UI thread so the window stays responsive.
        threading.Thread(target=self._worker, args=(jobs, s, len(skip)), daemon=True).start()

    def _set_busy(self, busy):
        self.running = busy
        self.go_btn.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")
        for b in self.edit_btns:
            b.config(state="disabled" if busy else "normal")
        if busy:
            self.progress.config(value=0)

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _check_cancel(self):
        if self.cancelled:
            raise Cancelled()

    def _set_proc(self, proc):
        self.proc = proc
        if self.cancelled:  # Cancel was pressed just before it started
            proc.terminate()

    def _worker(self, jobs, s, skipped):
        n = len(jobs)
        results = dict(done=0, failed=0, cancelled=0)
        first_out = None
        if not self.engine:
            try:
                def on_progress(done, total):
                    self.ui(lambda: (self.progress.config(value=done / total * 1000),
                                     self.status.config(text=f"Downloading the upscaler: {done / 1e6:.0f} of "
                                                             f"{total / 1e6:.0f} MB…")))
                download_engine(on_progress, self._check_cancel)
                self.engine = find_engine()
                if not self.engine:
                    raise DownloadError("The download didn't contain the upscaler program.")
            except Cancelled:
                self.ui(lambda: self._finish_early("Canceled."))
                return
            except Exception as e:
                log_error(f"Download: {type(e).__name__}: {e}")
                msg = str(e) if isinstance(e, DownloadError) else (
                    "Couldn't download the upscaler. Check your internet connection and try again.")
                self.ui(lambda: self._finish_early(msg, error=True))
                return
        tmp = tempfile.mkdtemp(prefix="upscaler-")
        try:
            for k, (iid, src, out) in enumerate(jobs):
                if self.cancelled:
                    self.ui(lambda i=iid: self._set_row(i, status="Canceled"))
                    results["cancelled"] += 1
                    continue
                name = os.path.basename(src)

                def on_progress(frac, i=iid, k=k, name=name):
                    self.ui(lambda: (self._set_row(i, status=f"Upscaling {frac * 100:.0f}%"),
                                     self.progress.config(value=(k + frac) / n * 1000),
                                     self.status.config(text=f"Image {k + 1} of {n}: {name}")))
                on_progress(0.0)
                t0 = time.monotonic()
                try:
                    png, alpha, icc = prepare_input(src, tmp)
                    up = os.path.join(tmp, "out.png")
                    if os.path.exists(up):
                        os.remove(up)
                    log = run_engine(self.engine, png, up, s["model"], on_progress, self._set_proc)
                    if not self.gpu and gpu_name(log):
                        self.gpu = gpu_name(log)
                    self._check_cancel()
                    self.ui(lambda i=iid: self._set_row(i, status="Saving…"))
                    data, size = finish_output(up, s["scale"], s["fmt"], icc)
                    part = out + ".part"
                    try:
                        with open(part, "wb") as f:
                            f.write(data)
                        os.replace(part, out)  # a cancel or crash never leaves half a file
                    finally:
                        if os.path.exists(part):
                            os.remove(part)
                    status = f"Done in {time.monotonic() - t0:.1f} s — {size[0]}×{size[1]}, {fmt_size(len(data))}"
                    results["done"] += 1
                    first_out = first_out or out
                except Cancelled:
                    status = "Canceled"
                    results["cancelled"] += 1
                except EngineError as e:
                    status = "Canceled" if self.cancelled else f"Failed: {e}"
                    results["cancelled" if self.cancelled else "failed"] += 1
                    if not self.cancelled:
                        log_error(f"{src}\n{e}")
                except Exception as e:  # keep going with the other images
                    log_error(f"{src}\n{type(e).__name__}: {e}")
                    status = f"Failed: {explain_error(e)}"
                    results["failed"] += 1
                self.ui(lambda i=iid, st=status: self._set_row(i, status=st))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.ui(lambda: self._finish(results, skipped, first_out))

    def _finish_early(self, text, error=False):
        self._set_busy(False)
        for iid in self.tree.get_children():
            if self.tree.set(iid, "status") == "Waiting":
                self._set_row(iid, status="Ready")
        self.status.config(text=text)
        if error:
            messagebox.showerror("Upscaler problem", text)

    def _finish(self, r, skipped, first_out):
        self._set_busy(False)
        self.progress.config(value=1000 if r["done"] else 0)
        parts = [f"{r['done']} upscaled"]
        if skipped:
            parts.append(f"{skipped} skipped (already existed)")
        if r["failed"]:
            parts.append(f"{r['failed']} failed — see the list")
        if r["cancelled"]:
            parts.append(f"{r['cancelled']} canceled")
        text = ("Done! " if not (r["failed"] or r["cancelled"]) else "") + ", ".join(parts) + "."
        if self.gpu:
            text += f" Using {self.gpu}."
        self.status.config(text=text, foreground="")
        if first_out and self.v["reveal"].get() and not self.cancelled:
            reveal_file(first_out)

    def _report_error(self, exc, val, tb):
        import traceback
        self._set_busy(False)
        detail = "".join(traceback.format_exception(exc, val, tb))
        log_error(detail)
        self.status.config(text=f"Unexpected error: {val}")
        messagebox.showerror("Unexpected error", detail[-1500:])

    def on_close(self):
        self.cancel()
        self.root.destroy()


LOG_PATH = os.path.join(tempfile.gettempdir(), "image_upscaler_errors.log")


def log_error(text):
    """Append an error to a log file. Errors can otherwise vanish when the
    script runs without a console (double-clicked / pythonw)."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{text}\n")
    except OSError:
        pass


def reveal_file(path):
    """Open the file's folder with the file selected."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception:
        pass


def self_test():
    """Used by the release build: exit code 0 if the bundled program, its models,
    Pillow and drag and drop are all in the packaged app. (Build machines have
    no graphics chip, so the program itself is only checked to start.)"""
    try:
        if load_libraries():
            return 1
        engine = find_engine()
        if not engine:
            return 2
        for m in MODELS.values():
            for ext in (".param", ".bin"):
                if not os.path.isfile(os.path.join(engine, "models", m["name"] + ext)):
                    return 3
        r = subprocess.run([os.path.join(engine, ENGINE_EXE), "-h"], capture_output=True, timeout=60,
                           **_POPEN_FLAGS)
        if b"realesrgan-ncnn-vulkan" not in r.stdout + r.stderr:
            return 4
        root = tk.Tk()
        root.withdraw()
        from tkinterdnd2 import TkinterDnD
        TkinterDnD.require(root)
        root.destroy()
        return 0
    except Exception as e:
        log_error(f"Self-test failed: {type(e).__name__}: {e}")
        return 5


SUPPORT_URL = "https://ko-fi.com/collinbrooksdev"


def support_link(parent, bg=None, font=None):
    """A small "Support this project" link that opens the Ko-fi page in the browser."""
    import webbrowser
    kw = {"bg": bg} if bg else {}
    link = tk.Label(parent, text="\u2665 Support this project", fg="#c8364f", cursor="hand2",
                    font=font or ("", 9, "underline"), **kw)
    link.bind("<Button-1>", lambda e: webbrowser.open(SUPPORT_URL))
    return link


# --------------------------------------------------------------------------- #
#  The look (shared by the Offline Toolkit apps): white cards, a dark purple
#  header fading to blue, blue actions. Only colors and fonts: no behavior.
# --------------------------------------------------------------------------- #
THEME = dict(
    bg="#ffffff", card="#ffffff", border="#e2ddf0", text="#1c1433", muted="#6e6787", error="#c8364f",
    purple="#2b1655", purple2="#4a2a8a", blue="#2f7cf6", blue_hover="#1d67dc", blue_soft="#e7f0ff",
    blue_text="#1756c4", lavender_line="#7b5cd6", disabled="#c9c4d8", tint="#f5f1ff")
MUTED, ERROR = THEME["muted"], THEME["error"]


def ui_family():
    try:
        import tkinter.font as tkfont
        families = set(tkfont.families())
    except tk.TclError:
        return "TkDefaultFont"
    for name in ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Inter", "Cantarell", "DejaVu Sans"):
        if name in families:
            return name
    return "TkDefaultFont"


def _indicator_image(root, kind, on, enabled, n=18, k=4):
    """A checkbox (a blue box with a white tick) or radio button (a blue ring and dot),
    drawn pixel by pixel with smooth edges (no image library needed)."""
    t = THEME
    blue = t["blue"] if enabled else t["disabled"]
    edge = "#b9b0d6" if enabled else t["disabled"]

    def rgb(h):
        return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))

    def seg_dist(px, py, ax, ay, bx, by):
        dx, dy = bx - ax, by - ay
        u = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return ((px - ax - u * dx) ** 2 + (py - ay - u * dy) ** 2) ** 0.5

    def in_round_rect(x, y, r=4.6, lo=0.9, hi=n - 0.9):
        cx, cy = min(max(x, lo + r), hi - r), min(max(y, lo + r), hi - r)
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r

    def colour_at(x, y):
        """The color of one sample point, or None where it's see-through."""
        c = n / 2
        d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
        if kind == "check":
            if not in_round_rect(x, y):
                return None
            if on:
                tick = min(seg_dist(x, y, 4.8, 9.4, 7.8, 12.4), seg_dist(x, y, 7.8, 12.4, 13.4, 5.9))
                return "#ffffff" if tick <= 1.15 else blue
            return edge if not in_round_rect(x, y, 3.4, 2.3, n - 2.3) else "#ffffff"
        if d > n / 2 - 0.9:
            return None
        if on:
            return blue if d > n / 2 - 2.5 or d < 3.6 else "#ffffff"
        return edge if d > n / 2 - 2.3 else "#ffffff"

    img = tk.PhotoImage(master=root, width=n, height=n)
    for py in range(n):
        row = []
        for px in range(n):
            acc, hits = [0, 0, 0], 0
            for sy in range(k):
                for sx in range(k):
                    col = colour_at(px + (sx + 0.5) / k, py + (sy + 0.5) / k)
                    if col is not None:
                        r, g, b = rgb(col)
                        acc[0] += r
                        acc[1] += g
                        acc[2] += b
                        hits += 1
            if hits == 0:
                row.append(None)
                continue
            # Blend with white for partly covered edge pixels (the widgets sit on white).
            a = hits / (k * k)
            r, g, b = (round(acc[i] / hits * a + 255 * (1 - a)) for i in range(3))
            row.append("#%02x%02x%02x" % (r, g, b))
        for px, col in enumerate(row):
            if col is not None:
                img.put(col, (px, py))
    return img


def apply_theme(root):
    """ttk's "clam" theme, recoloured (the native Windows and macOS themes ignore colors)."""
    import tkinter.font as tkfont
    t, fam = THEME, ui_family()
    root.configure(bg=t["bg"])
    root.option_add("*Background", t["bg"])  # plain Tk widgets (pictures, links) sit on white too
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        try:
            tkfont.nametofont(name).configure(family=fam, size=10)
        except tk.TclError:
            pass
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=t["bg"], foreground=t["text"], font=(fam, 10), bordercolor=t["border"],
                focuscolor=t["blue"], lightcolor=t["card"], darkcolor=t["border"], troughcolor="#ece8f7")
    s.map(".", foreground=[("disabled", t["disabled"])])
    s.map("TLabel", background=[("disabled", t["card"])])  # clam grays a disabled label's background
    s.configure("TLabelframe", background=t["card"], bordercolor=t["border"], lightcolor=t["border"],
                darkcolor=t["border"], relief="solid", borderwidth=1)
    s.configure("TLabelframe.Label", background=t["card"], foreground=t["purple2"], font=(fam, 11, "bold"))
    for kind in ("TCheckbutton", "TRadiobutton"):
        s.configure(kind, background=t["card"], foreground=t["text"])
        s.map(kind, background=[("active", t["card"])], foreground=[("disabled", t["disabled"])])
    s.configure("TCombobox", fieldbackground="#ffffff", background="#ffffff", foreground=t["text"],
                arrowcolor=t["purple2"], bordercolor=t["border"], padding=(6, 3), selectbackground="#ffffff",
                selectforeground=t["text"])
    s.map("TCombobox", fieldbackground=[("readonly", "#ffffff"), ("disabled", "#f6f4fb")],
          foreground=[("disabled", t["disabled"])], arrowcolor=[("disabled", t["disabled"])],
          bordercolor=[("focus", t["blue"]), ("hover", t["lavender_line"])])
    s.configure("TEntry", fieldbackground="#ffffff", bordercolor=t["border"], padding=(6, 3))
    s.map("TEntry", bordercolor=[("focus", t["blue"])], fieldbackground=[("disabled", "#f6f4fb")])
    s.configure("TSpinbox", fieldbackground="#ffffff", bordercolor=t["border"], arrowcolor=t["purple2"], padding=(6, 3))
    s.configure("TButton", background="#ffffff", foreground=t["purple2"], bordercolor=t["border"],
                padding=(12, 5), font=(fam, 10, "bold"), relief="flat")
    s.map("TButton", background=[("disabled", t["card"]), ("pressed", t["tint"]), ("active", "#f7f4ff")],
          foreground=[("disabled", t["disabled"])], bordercolor=[("active", t["lavender_line"])])
    s.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", foreground=t["text"],
                rowheight=26, font=(fam, 10))
    s.map("Treeview", background=[("selected", t["blue_soft"])], foreground=[("selected", t["blue_text"])])
    s.configure("Treeview.Heading", background="#f6f4fb", foreground=t["muted"], font=(fam, 9, "bold"),
                relief="flat", padding=(6, 4))
    s.map("Treeview.Heading", background=[("active", "#efeaf9")])
    s.configure("Horizontal.TProgressbar", troughcolor="#ece8f7", background=t["blue"], bordercolor="#ece8f7",
                lightcolor=t["blue"], darkcolor=t["blue"])
    for orient in ("Vertical", "Horizontal"):
        s.configure(f"{orient}.TScrollbar", background="#ddd6ee", troughcolor="#f6f4fb", bordercolor="#f6f4fb",
                    arrowcolor=t["muted"], lightcolor="#ddd6ee", darkcolor="#ddd6ee", gripcount=0)
    s.configure("Horizontal.TScale", background=t["blue"], troughcolor="#ece8f7", bordercolor=t["border"])
    root.option_add("*TCombobox*Listbox.background", "#ffffff")
    root.option_add("*TCombobox*Listbox.foreground", t["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", t["blue"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    # Checkboxes and radio buttons: drawn ticks and dots (clam draws a tick as an X).
    imgs = root._indicator_images = {(kind, on, en): _indicator_image(root, kind, on, en)
                                     for kind in ("check", "radio") for on in (False, True) for en in (False, True)}
    for kind, widget in (("check", "Checkbutton"), ("radio", "Radiobutton")):
        name = f"Toolkit.{kind}.indicator"
        try:
            s.element_create(name, "image", imgs[(kind, False, True)],
                             ("disabled", "selected", imgs[(kind, True, False)]),
                             ("disabled", imgs[(kind, False, False)]),
                             ("selected", imgs[(kind, True, True)]), padding=(0, 0, 6, 0), sticky="")
        except tk.TclError:
            pass
        s.layout(f"T{widget}", [(f"{widget}.padding", {"sticky": "nswe", "children": [
            (name, {"side": "left", "sticky": ""}),
            (f"{widget}.focus", {"side": "left", "sticky": "w", "children": [
                (f"{widget}.label", {"sticky": "nswe"})]})]})])
    return fam


class PillButton(tk.Canvas):
    """A rounded blue button for the app's main action. Accepts .config(state=..., text=...)
    like a ttk.Button, so the rest of the app drives it the same way."""

    def __init__(self, parent, text, command, height=36, padx=24):
        import tkinter.font as tkfont
        self._font = tkfont.Font(family=ui_family(), size=11, weight="bold")
        self._padx = padx
        self._bh = height
        self._bw = self._font.measure(text) + 2 * padx
        super().__init__(parent, width=self._bw, height=height, bg=THEME["card"], highlightthickness=0, bd=0,
                         cursor="hand2")
        self._text, self._command, self._enabled, self._hover = text, command, True, False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        self._draw()

    def _draw(self):
        self.delete("all")
        t = THEME
        fill = t["disabled"] if not self._enabled else t["blue_hover"] if self._hover else t["blue"]
        r, w, h = self._bh // 2, self._bw, self._bh
        pts = [1 + r, 1, w - 1 - r, 1, w - 1, 1, w - 1, 1 + r, w - 1, h - 1 - r, w - 1, h - 1, w - 1 - r, h - 1,
               1 + r, h - 1, 1, h - 1, 1, h - 1 - r, 1, 1 + r, 1, 1]
        self.create_polygon(pts, smooth=True, fill=fill, outline=fill)
        self.create_text(w / 2, h / 2, text=self._text, fill="#ffffff", font=self._font)

    def _set_hover(self, on):
        self._hover = on
        self._draw()

    def _click(self, event):
        if self._enabled and 0 <= event.x <= self._bw and 0 <= event.y <= self._bh:
            self._command()

    def configure(self, cnf=None, **kw):
        if "state" in kw:
            self._enabled = kw.pop("state") != "disabled"
            super().configure(cursor="hand2" if self._enabled else "")
        if "text" in kw:
            self._text = kw.pop("text")
            self._bw = self._font.measure(self._text) + 2 * self._padx
            super().configure(width=self._bw)
        if "command" in kw:
            self._command = kw.pop("command")
        if cnf or kw:
            super().configure(cnf, **kw)
        self._draw()

    config = configure

    def cget(self, key):
        if key == "state":
            return "normal" if self._enabled else "disabled"
        if key == "text":
            return self._text
        return super().cget(key)

    def invoke(self):
        if self._enabled:
            self._command()


def app_header(parent, title, tagline, logo):
    """The app's bar: dark purple fading to blue, with its logo, name and what it does."""
    t, fam = THEME, ui_family()
    c = tk.Canvas(parent, height=60, highlightthickness=0, bd=0, bg=t["purple"])

    def draw(event=None):
        c.delete("all")
        w = max(c.winfo_width(), 300)
        a, b = (0x2b, 0x16, 0x55), (0x2f, 0x5c, 0xd6)
        steps = 48
        for i in range(steps):
            k = (i / (steps - 1)) ** 2.2  # mostly purple, turning blue on the right
            col = "#%02x%02x%02x" % tuple(round(a[j] + (b[j] - a[j]) * k) for j in range(3))
            c.create_rectangle(i * w / steps, 0, (i + 1) * w / steps + 1, 60, fill=col, outline=col)
        if not hasattr(c, "_logo"):  # the app's own icon
            c._logo = tk.PhotoImage(master=c, data=HEADER_ICON)
        c.create_image(35, 30, image=c._logo)
        c.create_text(68, 21, text=title, anchor="w", fill="#ffffff", font=(fam, 15, "bold"))
        c.create_text(69, 42, text=tagline, anchor="w", fill="#cfc6ff", font=(fam, 9))
    c.bind("<Configure>", draw)
    return c


# The app's icon (64 px PNG), for the window and the taskbar. The .exe and the Mac app get
# theirs from assets/ when they're built.
APP_ICON = (
    "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"
)

# The same icon, small, for the app's header bar.
HEADER_ICON = (
    "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"
)


def set_app_icon(root):
    try:
        if sys.platform == "win32":
            # Its own taskbar button and icon, not Python's, when run as a script.
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OfflineToolkit.ImageUpscaler")
        root._app_icon = tk.PhotoImage(master=root, data=APP_ICON)
        root.iconphoto(True, root._app_icon)
    except Exception:
        pass  # just the default icon


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    root = tk.Tk()
    apply_theme(root)
    set_app_icon(root)
    UpscalerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
