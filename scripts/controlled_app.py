"""Controlled browser app (§22.15/§22.16): input #f for the deployed
takeover journey. Runs INSIDE the api container on its own loopback."""
import http.server
import threading
import time


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = (b"<!doctype html><html><body><h1 id='h'>Controlled App</h1>"
                b"<input id='f' name='field'/></body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 8777), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(3600)
