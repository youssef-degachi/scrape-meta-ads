#!/usr/bin/env python3
"""
Local website to browse scraped advertisers, mark them as contacted, and
start new scrapes.

    python serve.py            # then open http://localhost:8000

Each collection lives in data/<name>/ (ads.json, pages.json, ads.csv,
pages.csv, <name>.xlsx, meta.json, scrape.log). "Contacted" checkboxes and
notes are saved in data/contacted.json (shared across collections, because
the same clinic can appear in several searches).
"""

import json
import subprocess
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
WEB = ROOT / "web"
STATE = DATA / "contacted.json"
PYTHON = sys.executable
RUNNING = {}  # name -> Popen


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    DATA.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def collections():
    out = []
    if not DATA.exists():
        return out
    for d in sorted(DATA.iterdir()):
        if not d.is_dir():
            continue
        meta = read_json(d / "meta.json", {"name": d.name, "status": "done" if (d / "ads.json").exists() else "unknown"})
        meta["name"] = d.name
        proc = RUNNING.get(d.name)
        if proc is not None and proc.poll() is None:
            meta["status"] = "running"
        elif meta.get("status") == "running":
            # a previous server run died mid-scrape
            meta["status"] = "done" if (d / "ads.json").exists() else "error"
        log = d / "scrape.log"
        if log.exists():
            try:
                tail = log.read_text(encoding="utf-8", errors="ignore").replace("\r", "\n").strip().split("\n")
                meta["progress"] = tail[-1][:200] if tail else ""
            except Exception:
                pass
        meta["has_data"] = (d / "ads.json").exists()
        xl = d / f"{d.name}.xlsx"
        meta["xlsx"] = f"/files/{d.name}/{xl.name}" if xl.exists() else None
        out.append(meta)
    out.sort(key=lambda m: m.get("started") or "", reverse=True)
    return out


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB), **kw)

    def log_message(self, fmt, *args):
        return

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path):
        if not path.is_file():
            return self._json({"error": "not found"}, 404)
        data = path.read_bytes()
        ctype = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 "csv": "text/csv; charset=utf-8", "json": "application/json; charset=utf-8",
                 "log": "text/plain; charset=utf-8"}.get(path.suffix.lstrip("."), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/collections":
            return self._json(collections())
        if u.path == "/api/data":
            name = q.get("name", [""])[0]
            d = DATA / name
            if not name or "/" in name or ".." in name or not (d / "ads.json").exists():
                return self._json({"error": "unknown collection"}, 404)
            return self._json({"ads": read_json(d / "ads.json", []), "pages": read_json(d / "pages.json", []),
                               "meta": read_json(d / "meta.json", {})})
        if u.path == "/api/contacted":
            return self._json(load_state())
        if u.path.startswith("/files/"):
            parts = u.path.split("/")[2:]
            if len(parts) != 2 or any(".." in p or not p for p in parts):
                return self._json({"error": "bad path"}, 400)
            return self._file(DATA / parts[0] / parts[1])
        if u.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)

        if u.path == "/api/contacted":
            key = str(payload.get("key", "")).strip()
            if not key:
                return self._json({"error": "missing key"}, 400)
            state = load_state()
            entry = state.get(key, {})
            if "contacted" in payload:
                entry["contacted"] = bool(payload["contacted"])
            if "note" in payload:
                entry["note"] = str(payload["note"])[:2000]
            if "when" in payload:
                entry["when"] = payload["when"]
            state[key] = entry
            save_state(state)
            return self._json({"ok": True, "entry": entry})

        if u.path == "/api/scrape":
            url = str(payload.get("url", "")).strip()
            if "facebook.com/ads/library" not in url:
                return self._json({"error": "That is not a Facebook Ads Library URL."}, 400)
            sys.path.insert(0, str(ROOT))
            from scrape_ads import default_name, slugify
            name = slugify(payload.get("name") or "") if payload.get("name") else default_name(url)
            if name in RUNNING and RUNNING[name].poll() is None:
                return self._json({"error": f"'{name}' is already running."}, 409)
            folder = DATA / name
            folder.mkdir(parents=True, exist_ok=True)
            cmd = [PYTHON, str(ROOT / "scrape_ads.py"), url, "--name", name, "--data-dir", str(DATA),
                   "--max-ads", str(int(payload.get("max_ads") or 2000))]
            if payload.get("deep"):
                cmd.append("--deep")
            log = open(folder / "scrape.log", "w", encoding="utf-8")
            RUNNING[name] = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT))
            return self._json({"ok": True, "name": name})

        return self._json({"error": "not found"}, 404)


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    try:
        srv = Server(("0.0.0.0", port), Handler)
    except OSError as e:
        if e.errno == 98:
            print(f"Port {port} is already in use. The site is probably already running: "
                  f"open http://localhost:{port}\nOr start on another port:  ./start.sh 8080")
            sys.exit(1)
        raise
    print(f"Serving on http://localhost:{port}  (Ctrl+C to stop)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
