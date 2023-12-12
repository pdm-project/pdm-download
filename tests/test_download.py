import functools
import os
from pathlib import Path

import pytest

pytest_plugins = ["pdm.pytest"]

PACKAGES = Path(__file__).parent / "packages"
PROJECT = Path(__file__).parent / "project"


@pytest.fixture(scope="session", autouse=True)
def local_file_server():
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    with ThreadingHTTPServer(
        ("127.0.0.1", 9876),
        functools.partial(SimpleHTTPRequestHandler, directory=PACKAGES),
    ) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield httpd
        finally:
            httpd.shutdown()
            thread.join()


@pytest.mark.parametrize("lockfile_options", [[], ["-L", "pdm.static.lock"]])
def test_download_packages(pdm, tmp_path, lockfile_options):
    old_cwd = os.getcwd()
    os.chdir(PROJECT)
    try:
        pdm(["download", "-d", str(tmp_path)] + lockfile_options, strict=True)
    finally:
        os.chdir(old_cwd)
    assert set(os.listdir(tmp_path)) == set(os.listdir(PACKAGES))
