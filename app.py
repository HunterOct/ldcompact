import os
import sys
import json
import time
import threading
import traceback
import shutil
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import webview
import ldc_core as core
import ldplayer

GB = 1024 ** 3
MB = 1024 ** 2
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
DEFAULT_CONFIG = {"theme": "auto", "lang": "en"}
_WIN = {"w": None}


def human(n):
    return round(n / GB, 2)


def now():
    return time.strftime("%H:%M:%S")


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        out = dict(DEFAULT_CONFIG)
        for k in DEFAULT_CONFIG:
            if k in data:
                out[k] = data[k]
        return out
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(cfg):
    try:
        cur = load_config()
        for k in DEFAULT_CONFIG:
            if k in cfg:
                cur[k] = cfg[k]
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cur, f, indent=2)
        return cur
    except Exception:
        return load_config()


def set_clipboard(text):
    try:
        import ctypes
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        k32.GlobalAlloc.restype = ctypes.c_void_p
        k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [ctypes.c_void_p]
        k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        u32.SetClipboardData.restype = ctypes.c_void_p
        u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        u32.OpenClipboard.argtypes = [ctypes.c_void_p]
        data = text.encode("utf-16-le") + b"\x00\x00"
        if not u32.OpenClipboard(None):
            return False
        try:
            u32.EmptyClipboard()
            h = k32.GlobalAlloc(0x0002, len(data))
            if not h:
                return False
            p = k32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            k32.GlobalUnlock(h)
            u32.SetClipboardData(13, h)
        finally:
            u32.CloseClipboard()
        return True
    except Exception:
        return False


class Api:
    def __init__(self):
        self.root = None
        self.busy = False

    # ---------- output ----------
    def _emit(self, evt):
        w = _WIN.get("w")
        if not w:
            return
        try:
            w.evaluate_js("window.__evt && window.__evt(%s)"
                          % json.dumps(evt, ensure_ascii=False))
        except Exception:
            pass

    def _log(self, text, level="info"):
        self._emit({"type": "log", "text": "[%s] %s" % (now(), text), "level": level})

    def _phase(self, name):
        self._emit({"type": "phase", "name": name})

    # ---------- misc ----------
    def copy_text(self, text):
        return {"ok": set_clipboard(text or "")}

    def get_config(self):
        return load_config()

    def set_config(self, cfg):
        return save_config(cfg or {})

    # ---------- scan ----------
    def scan(self, root=None):
        if root:
            self.root = root
        root = self.root or ldplayer.find_root()
        if not root or not os.path.isfile(os.path.join(root, ldplayer.EXE)):
            return {"ok": False, "error": "LDPlayer installation not found (ldconsole.exe)"}
        self.root = root
        try:
            insts = ldplayer.list_instances(root)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        tasks = []
        for it in insts:
            for f in it["files"]:
                if f["name"] != "system.vmdk":
                    tasks.append((it["index"], f))

        def work(t):
            idx, f = t
            try:
                return idx, f["name"], core.analyze(f["path"])
            except Exception as e:
                return idx, f["name"], {"err": str(e)}

        done = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            for idx, name, a in ex.map(work, tasks):
                done[(idx, name)] = a

        out = []
        for it in insts:
            files = []
            tot = rec = 0
            clean = True
            for f in it["files"]:
                a = done.get((it["index"], f["name"]))
                if f["name"] == "system.vmdk":
                    files.append({"name": f["name"], "size": human(f["size"]),
                                  "reclaim": 0.0, "after": human(f["size"]),
                                  "skip": True, "clean": True})
                    tot += f["size"]
                    continue
                if not a or "err" in a:
                    files.append({"name": f["name"], "size": human(f["size"]),
                                  "reclaim": 0.0, "after": human(f["size"]),
                                  "err": (a or {}).get("err", "?"), "clean": False})
                    tot += f["size"]
                    clean = False
                    continue
                r = a["reclaim"] * a["grain"]
                k = a["keep"] * a["grain"] + a["overhead"] * core.SECTOR
                clean = clean and a["clean"]
                tot += a["size"]
                rec += r
                files.append({"name": f["name"], "size": human(a["size"]),
                              "reclaim": human(r), "after": human(k),
                              "clean": a["clean"], "alloc": a["alloc"]})
            out.append(dict(index=it["index"], name=it["name"], running=it["running"],
                            total=human(tot), reclaim=human(rec),
                            after=human(tot - rec), files=files, clean=clean))
        free = 0
        try:
            free = shutil.disk_usage(root).free
        except Exception:
            pass
        return {"ok": True, "root": root, "free": human(free), "instances": out}

    # ---------- clean ----------
    def compact(self, indices, quit_running=False, keep_backup=False):
        if self.busy:
            return {"ok": False, "error": "Already running"}
        if not self.root:
            return {"ok": False, "error": "Not scanned yet"}
        self.busy = True
        threading.Thread(target=self._run, args=(indices, quit_running, keep_backup),
                         daemon=True).start()
        return {"ok": True}

    def _run(self, indices, quit_running, keep_backup):
        t_start = time.time()
        total_old = total_new = 0
        done = skipped = failed = 0
        try:
            self._log("=== START ===", "head")
            self._log("LDPlayer root: %s" % self.root)
            free = shutil.disk_usage(self.root).free
            self._log("Free space: %.2f GB | selected: %d | auto-close: %s | keep backup: %s"
                      % (free / GB, len(indices), "yes" if quit_running else "no",
                         "yes" if keep_backup else "no"))
            insts = ldplayer.list_instances(self.root)
            by_idx = {i["index"]: i for i in insts}
            self._log("Rescan: %d instance(s)" % len(insts))

            for idx in indices:
                it = by_idx.get(int(idx))
                if not it:
                    self._log("Instance #%s does not exist - skipped" % idx, "warn")
                    skipped += 1
                    continue
                self._log("=== %s (#%d) ===" % (it["name"], it["index"]), "head")
                self._log("  dir: %s" % it["dir"])
                if not os.path.isdir(it["dir"]):
                    self._log("  directory missing - skipped", "err")
                    failed += 1
                    continue
                if it["running"]:
                    if not quit_running:
                        self._log("  running - skipped (enable 'auto-close' to handle it)", "err")
                        skipped += 1
                        continue
                    self._log("  running -> calling ldconsole quit...", "warn")
                    if not ldplayer.quit_instance(self.root, it["index"], it["name"]):
                        self._log("  could not shut down within 90s - skipped", "err")
                        failed += 1
                        continue
                    self._log("  instance stopped")
                    time.sleep(2)
                else:
                    self._log("  state: stopped")

                ok_any = False
                for f in it["files"]:
                    if f["name"] == "system.vmdk":
                        self._log("  system.vmdk - skipped (nothing to reclaim)")
                        continue
                    r = self._compact_file(it, f, keep_backup)
                    if r == "ok":
                        ok_any = True
                        total_old += f["_old"]
                        total_new += f["_new"]
                    elif r == "fail":
                        failed += 1
                if ok_any:
                    done += 1

            free2 = shutil.disk_usage(self.root).free
            self._log("=== DONE ===", "head")
            self._log("  cleaned %d | skipped %d | failed %d | %.1fs"
                      % (done, skipped, failed, time.time() - t_start))
            self._log("  %.2f GB -> %.2f GB (saved %.2f GB)"
                      % (human(total_old), human(total_new),
                         human(max(0, total_old - total_new))), "ok")
            self._log("  drive free: %.2f GB -> %.2f GB" % (free / GB, free2 / GB))
            self._emit({"type": "done", "ok": True, "failed": failed,
                        "saved": human(max(0, total_old - total_new)),
                        "old": human(total_old), "new": human(total_new)})
        except Exception as e:
            self._log("UNHANDLED ERROR: %s" % e, "err")
            for line in traceback.format_exc().splitlines()[-6:]:
                self._log("  " + line, "err")
            self._emit({"type": "done", "ok": False, "error": str(e)})
        finally:
            self.busy = False

    # ---------- guards ----------
    def _guard_file(self, path, need):
        name = os.path.basename(path)
        if not os.path.isfile(path):
            self._log("  %s: file missing" % name, "err")
            return False, None, "missing"
        try:
            h = core.header_info(path)
        except Exception as e:
            self._log("  %s: unreadable header (%s)" % (name, e), "err")
            return False, None, "header"
        bad = []
        if h["version"] != 1:
            bad.append("version=%d (only 1 supported)" % h["version"])
        if h["compress"] != 0:
            bad.append("compressAlgorithm=%d (must be 0)" % h["compress"])
        g = h["grain"]
        if g < 8 or (g & (g - 1)):
            bad.append("grain=%d is not a power of two" % g)
        n = h["ngte"]
        if n < 128 or n > 512 or (n & (n - 1)):
            bad.append("numGTEsPerGT=%d invalid" % n)
        if h["overhead"] * core.SECTOR > h["size"]:
            bad.append("overhead exceeds file size")
        if bad:
            for b in bad:
                self._log("  %s: GUARD FAIL - %s" % (name, b), "err")
            return False, h, "header"
        self._log("  %s: header OK (v%d, grain %d sectors, ngte %d, overhead %d)"
                  % (name, h["version"], g, n, h["overhead"]))

        try:
            a = core.analyze(path)
        except Exception as e:
            self._log("  %s: analysis failed (%s)" % (name, e), "err")
            return False, h, "analyze"
        if not a["states"]:
            self._log("  %s: no ext4 filesystem found" % name, "warn")
        elif not a["clean"]:
            self._log("  %s: GUARD FAIL - filesystem not clean (state=%s). "
                      "Boot the instance and shut it down properly first."
                      % (name, a["states"]), "err")
            return False, h, "dirty"
        else:
            for nt in a["notes"]:
                if "fs_gb" in nt:
                    self._log("  %s: ext4 %.2f GB | free %.2f GB | state=%d"
                              % (name, nt["fs_gb"], nt["free_gb"], nt["state"]))

        if a["reclaim"] == 0:
            self._log("  %s: no reclaimable grains - skipped" % name, "warn")
            return False, a, "nothing"
        self._log("  %s: %.2f GB | grains allocated %s | keep %s | drop %s"
                  % (name, a["size"] / GB, format(a["alloc"], ","),
                     format(a["keep"], ","), format(a["reclaim"], ",")))
        free = shutil.disk_usage(os.path.dirname(path)).free
        self._log("  %s: projected %.2f GB | need %.2f GB free | have %.2f GB"
                  % (name, need / GB, (need + 256 * MB) / GB, free / GB))
        if free < need + 256 * MB:
            self._log("  %s: GUARD FAIL - not enough free space" % name, "err")
            return False, a, "space"
        for leftover in (path + ".new", path + ".orig"):
            if os.path.exists(leftover):
                self._log("  %s: GUARD FAIL - leftover %s from a previous run"
                          % (name, os.path.basename(leftover)), "err")
                return False, a, "leftover"
        return True, a, None

    def _compact_file(self, it, f, keep_backup):
        path = f["path"]
        new = path + ".new"
        bak = path + ".orig"
        name = f["name"]
        self._phase(name)
        self._emit({"type": "progress", "file": name, "pct": 0})

        try:
            a0 = core.analyze(path)
            need = a0["keep"] * a0["grain"] + a0["overhead"] * core.SECTOR
        except Exception as e:
            self._log("  %s: cannot read (%s) - skipped" % (name, e), "err")
            return "fail"

        self._log("  -- %s --" % name)
        ok, a, why = self._guard_file(path, need)
        if not ok:
            return "skip" if why in ("nothing",) else "fail"

        t0 = time.time()
        try:
            self._log("  %s: writing new image (%s grains)..." % (name, format(a["keep"], ",")))

            def pg(n, tot):
                self._emit({"type": "progress", "file": name,
                            "pct": round(100.0 * n / max(1, tot), 1)})

            res = core.compact(path, new, verify=True, progress=pg)
            self._log("  %s: write finished in %.1fs | new size %.2f GB"
                      % (name, time.time() - t0, res["new"] / GB))

            t1 = time.time()
            errs = core.validate(new)
            if errs:
                raise core.VmdkError("validate: " + "; ".join(errs))
            self._log("  %s: validate OK (GT matches RGT, no stray grains) | %.1fs"
                      % (name, time.time() - t1))

            if os.path.exists(bak):
                os.remove(bak)
            os.replace(path, bak)
            self._log("  %s: old image renamed -> .orig" % name)
            try:
                os.replace(new, path)
            except Exception:
                os.replace(bak, path)
                raise
            self._log("  %s: new image moved into place" % name)

            f["_old"] = res["old"]
            f["_new"] = res["new"]
            if not keep_backup:
                os.remove(bak)
                self._log("  %s: backup removed" % name)
            self._emit({"type": "progress", "file": name, "pct": 100})
            self._log("  %s: DONE %.2f GB -> %.2f GB (saved %.2f GB) in %.1fs"
                      % (name, res["old"] / GB, res["new"] / GB,
                         (res["old"] - res["new"]) / GB, time.time() - t0), "ok")
            return "ok"
        except Exception as e:
            self._log("  %s: ERROR - %s" % (name, e), "err")
            for line in traceback.format_exc().splitlines()[-5:]:
                self._log("    " + line, "err")
            try:
                if os.path.exists(new):
                    os.remove(new)
                    self._log("  %s: partial .new removed" % name)
                if os.path.exists(bak) and not os.path.exists(path):
                    os.replace(bak, path)
                    self._log("  %s: original restored from .orig" % name)
            except Exception as e2:
                self._log("  %s: cleanup error - %s" % (name, e2), "err")
            return "fail"


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    html = open(os.path.join(HERE, "ui.html"), "r", encoding="utf-8").read()
    api = Api()
    window = webview.create_window(
        "LDCompact - LDPlayer disk cleaner",
        html=html, js_api=api, width=1300, height=780,
        min_size=(1000, 620), background_color="#101014")
    _WIN["w"] = window
    webview.start(debug=False)


if __name__ == "__main__":
    main()
