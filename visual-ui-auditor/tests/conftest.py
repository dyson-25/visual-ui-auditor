import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def fixture_url():
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

    handler = partial(Handler, directory=str(Path(__file__).parent / "fixtures"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/layout.html"
    server.shutdown()
    server.server_close()
    thread.join()
