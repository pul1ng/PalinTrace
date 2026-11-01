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


LIFECYCLE = '''import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

root = Path(__file__).parent
config = json.loads((root / "config.json").read_text())
identity = root / "service.json"

def process(pid):
    try:
        fields = Path("/proc/{}/stat".format(pid)).read_text().rsplit(")", 1)[1].split()
        return {"state": fields[0], "start": fields[19]}
    except FileNotFoundError:
        return None

def port_closed():
    try:
        with socket.create_connection((config["host"], config["port"]), timeout=0.5):
            return False
    except ConnectionRefusedError:
        return True

def ready():
    connection = http.client.HTTPConnection(config["host"], config["port"], timeout=0.5)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        return response.status == 200 and response.read().decode() == config["body"]
    except OSError:
        return False
    finally:
        connection.close()

if sys.argv[1] == "start":
    if not port_closed():
        raise RuntimeError("fixture port already in use")
    child = subprocess.Popen([sys.executable, "-B", str(root / "server.py")],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while True:
            if child.poll() is not None:
                raise RuntimeError("service exited before readiness")
            if ready() and child.poll() is None:
                identity.write_text(json.dumps({"pid": child.pid,
                                                "start": process(child.pid)["start"]}))
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("service readiness deadline exceeded")
            time.sleep(0.05)
    except BaseException:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
        raise
    print("PALIN_SERVICE_READY")
elif sys.argv[1] == "stop":
    saved = json.loads(identity.read_text())
    current = process(saved["pid"])
    if current is None or current["start"] != saved["start"] or current["state"] == "Z":
        raise RuntimeError("this fixture service is not running")
    os.kill(saved["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 5
    while True:
        current = process(saved["pid"])
        exited = current is None or current["state"] == "Z"
        if exited and port_closed():
            identity.unlink()
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("service shutdown unconfirmed")
        time.sleep(0.05)
    print("PALIN_SERVICE_STOPPED")
else:
    raise ValueError("expected start or stop")
'''


def python_command(code):
    return f"{PYTHON} -B -c " + shlex.quote(code)


def fixture_files():
    return {"server.py": SERVER, "request.py": REQUEST, "lifecycle.py": LIFECYCLE,
            "config.json": json.dumps(CONFIG, sort_keys=True) + "\n"}


def create_files_command():
    return python_command(
        "from pathlib import Path; "
        f"directory = Path({DIRECTORY!r}); directory.mkdir(); "
        f"[(directory / name).write_text(body) for name, body in {fixture_files()!r}.items()]"
    )


REQUEST_COMMAND = f"{PYTHON} -B {DIRECTORY}/request.py"
START_COMMAND = f"{PYTHON} -B {DIRECTORY}/lifecycle.py start"
STOP_COMMAND = f"{PYTHON} -B {DIRECTORY}/lifecycle.py stop"
