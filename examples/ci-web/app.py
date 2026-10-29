import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
        else:
            body = b"not found\n"
            self.send_response(404)

        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    greeting = os.environ.get("APP_GREETING")
    if not greeting:
        print("ERROR: APP_GREETING is required", file=sys.stderr, flush=True)
        sys.exit(1)

    print(f"Greeting configured: {greeting}", flush=True)
    server = HTTPServer(("0.0.0.0", 8080), Handler)
    print("Listening on port 8080", flush=True)
    server.serve_forever()
