# Local stand-in for api.agenticcontrolplane.com. Logs every request (method, path,
# client header, tool name, hook event) to the file given as argv[2] and answers the
# way the real gateway does: allow on /govern/tool-use, pass on /govern/tool-output.
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

LOG = sys.argv[2]


class H(BaseHTTPRequestHandler):
    def _log(self, body):
        with open(LOG, "a") as f:
            f.write(json.dumps({
                "method": self.command,
                "path": self.path,
                "client": self.headers.get("X-GS-Client"),
                "ua": self.headers.get("User-Agent"),
                "body": body,
            }) + "\n")

    def _send(self, obj):
        out = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = raw
        self._log(body)
        if self.path.endswith("/tool-use"):
            self._send({"decision": "allow", "reason": "stub"})
        else:
            self._send({"action": "pass"})

    def do_GET(self):
        self._log(None)
        self._send({"ok": True})

    def do_HEAD(self):
        self._log(None)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
