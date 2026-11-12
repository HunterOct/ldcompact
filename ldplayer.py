"""Phat hien cai dat LDPlayer va liet ke cac instance."""
import os
import subprocess
import glob

EXE = "ldconsole.exe"


def _run(root, *args):
    exe = os.path.join(root, EXE)
    try:
        p = subprocess.run([exe] + list(args), cwd=root, capture_output=True,
                           timeout=30, creationflags=0x08000000)
        out = p.stdout.decode("utf-8", "replace")
        if not out.strip():
            out = p.stderr.decode("utf-8", "replace")
        return out
    except Exception as e:
        return ""


def find_root():
    cands = []
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\ChangZhi\LDPlayer"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\ChangZhi2\ldplayer9"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\ChangZhi2\ldplayer9")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for name in ("InstallDir", "InstallDir9", "Path", "install_dir"):
                        try:
                            v, _ = winreg.QueryValueEx(k, name)
                            if v:
                                cands.append(v)
                        except OSError:
                            pass
            except OSError:
                pass
    except Exception:
        pass

    for d in "CDEFGHIJ":
        for n in ("LDPlayer9", "LDplayer9", "LDPlayer", "LDPlayer9_lite"):
            cands.append("%s:\\LDPlayer\\%s" % (d, n))
            cands.append("%s:\\%s" % (d, n))

    seen = set()
    for c in cands:
        c = os.path.normpath(c)
        if c in seen:
            continue
        seen.add(c)
        if os.path.isfile(os.path.join(c, EXE)):
            return c

    for d in "CDEFGHIJ":
        for pat in ("%s:/*/ldconsole.exe" % d, "%s:/*/*/ldconsole.exe" % d):
            for hit in glob.glob(pat):
                return os.path.dirname(hit)
    return None


def list_instances(root):
    out = _run(root, "list2")
    running = set()
    for line in _run(root, "runninglist").splitlines():
        line = line.strip()
        if line:
            running.add(line)
    items = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        idx = int(parts[0])
        name = parts[1]
        d = os.path.join(root, "vms", "leidian%d" % idx)
        files = []
        for f in ("data.vmdk", "sdcard.vmdk", "system.vmdk"):
            p = os.path.join(d, f)
            if os.path.isfile(p):
                files.append(dict(name=f, path=p, size=os.path.getsize(p)))
        items.append(dict(index=idx, name=name, dir=d, files=files,
                          running=name in running))
    return items


def quit_instance(root, index, name=None, timeout=90):
    _run(root, "quit", "--index", str(index))
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        names = [l.strip() for l in _run(root, "runninglist").splitlines() if l.strip()]
        if not names:
            return True
        if name is not None and name not in names:
            return True
        time.sleep(2)
    return False
