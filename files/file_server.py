#!/usr/bin/env python3
"""File server for n8n Track Platform — extends /home/deb/n8n/files/file_server.py.

Allows:
  - GET  /read?path=<abs_or_relative>
  - POST /write?path=<abs_or_relative>
  - GET  /list?path=<dir>
CORS enabled. Port 19099.

Allowed roots:
  - /home/deb/n8n-track-platform/tracks
  - /home/deb/n8n-track-platform/runs
  - /home/deb/n8n-track-platform/config
  - /home/deb/n8n-track-platform/workflows/state  (compat)
"""

import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

BASE_DIR = Path("/home/deb/n8n-track-platform")
TRACKS_DIR = BASE_DIR / "tracks"
RUNS_DIR = BASE_DIR / "runs"
CONFIG_DIR = BASE_DIR / "config"
WORKFLOWS_STATE_DIR = BASE_DIR / "workflows" / "state"

# Ensure dirs exist
for _d in [TRACKS_DIR, RUNS_DIR, CONFIG_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

PORT = 19099

ALLOWED_ROOTS = [
    TRACKS_DIR.resolve(),
    RUNS_DIR.resolve(),
    CONFIG_DIR.resolve(),
]

# Also allow workflows/state if present
try:
    WORKFLOWS_STATE_DIR.mkdir(parents=True, exist_ok=True)
    ALLOWED_ROOTS.append(WORKFLOWS_STATE_DIR.resolve())
except Exception:
    pass


class FileHandler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        filepath = params.get("path", [None])[0]

        # Health check
        if parsed.path == "/health":
            self._json(200, {"status": "ok", "port": PORT, "allowed_roots": [str(p) for p in ALLOWED_ROOTS]})
            return

        if parsed.path == "/read" and filepath:
            full = self._resolve(filepath)
            if not full:
                self._json(404, {"error": "Invalid path", "path": filepath})
                return
            try:
                with open(full, "r", encoding="utf-8") as f:
                    data = json.loads(f.read())
                self._json(200, data)
            except FileNotFoundError:
                self._json(404, {"error": "File not found", "path": filepath})
            except json.JSONDecodeError as e:
                self._json(500, {"error": f"Invalid JSON: {e}"})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if parsed.path == "/list" and filepath:
            full = self._resolve_dir(filepath)
            if not full:
                self._json(404, {"error": "Invalid path", "path": filepath})
                return
            try:
                if not full.exists():
                    self._json(404, {"error": "Directory not found", "path": filepath})
                    return
                if not full.is_dir():
                    self._json(400, {"error": "Not a directory", "path": filepath})
                    return
                entries = []
                for child in sorted(full.iterdir()):
                    entries.append({
                        "name": child.name,
                        "type": "dir" if child.is_dir() else "file",
                        "path": str(child),
                    })
                self._json(200, {"path": str(full), "entries": entries})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        self._json(404, {"error": "Not found", "path": parsed.path})

    def do_POST(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        filepath = params.get("path", [None])[0]

        if parsed.path == "/write" and filepath:
            full = self._resolve(filepath)
            if not full:
                self._json(404, {"error": "Invalid path", "path": filepath})
                return
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length)
                body_str = body.decode("utf-8")
                # Log short preview
                preview = body_str[:300].replace("\n", " ")
                print(f"  POST {filepath} ({len(body_str)} bytes): {preview}")
                sys.stdout.flush()
                data = json.loads(body_str) if body_str.strip() else {}

                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as f:
                    f.write(json.dumps(data, indent=2, ensure_ascii=False))

                self._json(200, {"status": "ok", "path": str(full)})
            except json.JSONDecodeError as e:
                self._json(400, {"error": f"Invalid JSON body: {e}"})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        self._json(404, {"error": "Not found", "path": parsed.path})

    def _resolve(self, filepath: str):
        """Resolve filepath to absolute path if inside allowed roots.

        Accepts:
          - absolute path (/home/deb/n8n-track-platform/tracks/...)
          - relative to BASE_DIR (tracks/team-a/...)
          - /tracks/..., /runs/..., /config/...
        Returns absolute Path string or None if outside allowed roots.
        """
        # Normalize input: if starts with /tracks, /runs, /config treat as relative to BASE_DIR
        p = filepath.strip()
        candidate = None
        if p.startswith("/home/deb/n8n-track-platform/"):
            candidate = Path(p)
        elif p.startswith("/tracks/") or p.startswith("/runs/") or p.startswith("/config/"):
            candidate = BASE_DIR / p.lstrip("/")
        elif p.startswith("tracks/") or p.startswith("runs/") or p.startswith("config/"):
            candidate = BASE_DIR / p
        elif p.startswith("/"):
            # Try absolute
            candidate = Path(p)
        else:
            candidate = BASE_DIR / p

        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = Path(os.path.abspath(str(candidate)))

        # Allow parent directories of allowed roots for creation (e.g., new team dir)
        for root in ALLOWED_ROOTS:
            # allow exact root or subpath
            try:
                resolved.relative_to(root)
                return str(resolved)
            except ValueError:
                continue
            # Also allow direct children of BASE_DIR that are allowed roots' subdirs
        # Check if resolved is inside BASE_DIR/tracks|runs|config even if root not yet resolved due to non-existent path
        # Re-check with absolute string prefix
        abs_str = str(resolved)
        for root in ALLOWED_ROOTS:
            if abs_str.startswith(str(root)):
                return abs_str
        # Also allow BASE_DIR itself for list
        if abs_str == str(BASE_DIR.resolve()):
            return abs_str
        return None

    def _resolve_dir(self, filepath: str):
        """Resolve directory path for listing. Allows BASE_DIR and allowed roots."""
        # Special: empty or "/" -> BASE_DIR
        if not filepath or filepath == "/":
            return BASE_DIR.resolve()
        resolved_str = self._resolve(filepath)
        if resolved_str:
            return Path(resolved_str)
        # Allow listing BASE_DIR subdirs even if not strictly inside allowed roots?
        # For listing, allow BASE_DIR itself
        candidate = BASE_DIR / filepath.lstrip("/")
        try:
            resolved = candidate.resolve()
            # Permit listing of tracks/, runs/, config/, workflows/
            if str(resolved).startswith(str(BASE_DIR.resolve())):
                return resolved
        except Exception:
            pass
        return None

    def _json(self, code: int, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # args[0] is method, args[1] is status code?
        # Use default formatting but print to stdout
        print(f"[{self.log_date_time_string()}] {self.address_string()} \"{args[0]}\" {args[1] if len(args) > 1 else ''}")
        sys.stdout.flush()


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), FileHandler)
    print(f"Track Platform file server listening on port {PORT}...")
    print(f"Allowed roots: {', '.join(str(p) for p in ALLOWED_ROOTS)}")
    print(f"Base dir: {BASE_DIR}")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()
