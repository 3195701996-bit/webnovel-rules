"""Owned loopback server for local runtime checks; no production data imports."""
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CHILD = """
import os
from werkzeug.serving import make_server
import app
@app.app.after_request
def tag_instance(response):
    response.headers['X-WR-Test-Instance'] = os.environ['WR_TEST_INSTANCE']
    return response
server = make_server('127.0.0.1', int(os.environ['PORT']), app.app,
                     threaded=True, fd=int(os.environ['WR_TEST_FD']))
server.serve_forever()
"""


class LocalServer:
    def __init__(self, data_dir, sources_dir):
        self.data_dir = str(Path(data_dir).resolve())
        self.sources_dir = str(Path(sources_dir).resolve())
        self.token = secrets.token_hex(16)
        self.process = None
        self.log = None
        self.port = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __enter__(self):
        if os.name != 'posix':
            raise RuntimeError('This runtime harness requires POSIX descriptor passing')
        sock = socket.socket()
        try:
            sock.bind(('127.0.0.1', 0))
            sock.listen(128)
            self.port = sock.getsockname()[1]
            env = dict(os.environ, WR_DATA_DIR=self.data_dir,
                       WR_SOURCES_DIR=self.sources_dir, WR_AUTH_PASSWORD='',
                       WR_DISABLE_BACKGROUND='1', WR_TEST='1',
                       PYTHONDONTWRITEBYTECODE='1', PORT=str(self.port),
                       WR_TEST_INSTANCE=self.token, WR_TEST_FD=str(sock.fileno()))
            self.log = tempfile.TemporaryFile(mode='w+b')
            self.process = subprocess.Popen(
                [sys.executable, '-c', CHILD], cwd=str(ROOT), env=env,
                pass_fds=(sock.fileno(),), stdout=self.log, stderr=self.log,
            )
        except BaseException:
            self.close()
            raise
        finally:
            sock.close()
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError('Owned server exited during startup: ' + self.output())
                try:
                    status, _, headers = self.request('GET', '/api/health', timeout=1)
                    if status == 200 and headers.get('X-WR-Test-Instance') == self.token:
                        return self
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(0.05)
            raise RuntimeError('Owned server startup timed out: ' + self.output())
        except BaseException:
            self.close()
            raise

    def request(self, method, path, body=None, timeout=20):
        request = urllib.request.Request(
            f'http://127.0.0.1:{self.port}{path}', data=body, method=method,
            headers={'Content-Type': 'application/json'},
        )
        try:
            response = self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            headers = response.headers
            if headers.get('X-WR-Test-Instance') != self.token:
                raise RuntimeError('Response is not from the owned test server')
            return response.code, response.read(), dict(headers)

    def output(self):
        if self.log is None:
            return ''
        self.log.seek(0)
        return self.log.read().decode('utf-8', 'replace')[-12000:]

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        if self.log is not None:
            self.log.close()
            self.log = None

    def __exit__(self, *exc):
        self.close()
