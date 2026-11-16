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
        about="For anime, cartoons, illustrations and other drawn art: clean lines and flat colours. "
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
    up (it ignores the rotation tag). Returns (png path, has transparency, colour profile)."""
    with Image.open(path) as im:
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        im.load()
        im = ImageOps.exif_transpose(im)
    icc = im.info.get("icc_profile") if im.mode not in ("CMYK", "L", "LA", "I;16", "I", "F") else None
    if im.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        div = 256 if im.mode.startswith("I;16") else 1
        im = im.convert("I").point(lambda v: v / div).convert("L")  # 16-bit grey: scale, don't clip
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
                    text=f"The upscaler can't run on this computer: {explain_error(e)}", foreground="red"))
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
        main = ttk.Frame(self.root, padding=10)
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
        self.list_lbl = ttk.Label(f, text="", foreground="gray")
        self.list_lbl.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        f = ttk.LabelFrame(main, text="2. Upscale", padding=8)
        f.grid(row=1, column=0, sticky="ew", **pad)
        ttk.Label(f, text="Model").grid(row=0, column=0, sticky="w", **pad)
        ttk.Combobox(f, values=list(MODELS), textvariable=self.v["model"], state="readonly",
                     width=42).grid(row=0, column=1, columnspan=3, sticky="w", **pad)
        self.model_lbl = ttk.Label(f, text="", foreground="gray", wraplength=620)
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
        self.go_btn = ttk.Button(f, text="Upscale", command=self.start)
        self.go_btn.grid(row=0, column=2, padx=(6, 0))
        self.status = ttk.Label(main, text="Ready.", wraplength=660)
        self.status.grid(row=4, column=0, sticky="w", **pad)

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
        self.list_lbl.config(text=text, foreground="gray")

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
                self.ui(lambda: self._finish_early("Cancelled."))
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
                    self.ui(lambda i=iid: self._set_row(i, status="Cancelled"))
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
                    status = "Cancelled"
                    results["cancelled"] += 1
                except EngineError as e:
                    status = "Cancelled" if self.cancelled else f"Failed: {e}"
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
            parts.append(f"{r['cancelled']} cancelled")
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


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    root = tk.Tk()
    try:  # nicer native look where available
        ttk.Style().theme_use("vista" if sys.platform == "win32" else
                              "aqua" if sys.platform == "darwin" else "clam")
    except tk.TclError:
        pass
    UpscalerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
