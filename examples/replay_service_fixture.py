"""Real missing-service Replay acceptance with fixed decisions and no model service."""

import json
import shlex

DIRECTORY = "/opt/palin-replay-service-fixture"
PYTHON = "/opt/miniconda3/bin/python"
ADDRESS = "http://127.0.0.1:18765/"
BODY = "PALIN_SERVICE_OK"
CONFIG = {"host": "127.0.0.1", "port": 18765, "body": BODY, "timeout_sec": 2}

SERVER = '''import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

config = json.loads(Path(__file__).with_name("config.json").read_text())

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = config["body"].encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass

HTTPServer((config["host"], config["port"]), Handler).serve_forever()
'''

REQUEST = '''import http.client
import json
import sys
from pathlib import Path

config = json.loads(Path(__file__).with_name("config.json").read_text())
connection = http.client.HTTPConnection(config["host"], config["port"],
                                        timeout=config["timeout_sec"])
try:
    connection.request("GET", "/")
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    print(json.dumps({"status": response.status, "body": body}, sort_keys=True))
    code = 0 if response.status == 200 and body == config["body"] else 2
except (OSError, http.client.HTTPException) as error:
    print(json.dumps({"error": type(error).__name__, "errno": getattr(error, "errno", None),
                      "message": str(error)}, sort_keys=True))
    code = 1
finally:
    connection.close()
sys.exit(code)
'''


def python_command(code):
    return f"{PYTHON} -B -c " + shlex.quote(code)


def fixture_files():
    return {"server.py": SERVER, "request.py": REQUEST,
            "config.json": json.dumps(CONFIG, sort_keys=True) + "\n"}


def create_files_command():
    return python_command(
        "from pathlib import Path; "
        f"directory = Path({DIRECTORY!r}); directory.mkdir(); "
        f"[(directory / name).write_text(body) for name, body in {fixture_files()!r}.items()]"
    )


REQUEST_COMMAND = f"{PYTHON} -B {DIRECTORY}/request.py"
