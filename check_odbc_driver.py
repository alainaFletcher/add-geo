"""Check that Microsoft ODBC Driver 18 actually loads (Docker build guard).

Runs as appuser in the final image (see Dockerfile), so it exercises what
production does: pyodbc's driver manager, appuser's file permissions, and
every library the driver links or opens at runtime.

It connects to a closed localhost port with no credentials, so it touches no
database. A loadable driver fails with a network error (08001 / HYT00); a
driver that is missing or can't be loaded fails with 01000 ("Can't open lib")
or IM002. On a load failure it prints `ldd` of the driver so the missing
library is named in the build log, and exits 1 to fail the build.

Manual use: docker run --rm address-geocoding:latest python check_odbc_driver.py
"""
import subprocess
import sys

import pyodbc

DRIVER = "ODBC Driver 18 for SQL Server"
DRIVER_NOT_LOADED_SQLSTATES = {"01000", "IM002"}


def _driver_path() -> str | None:
    """Return the driver .so path registered in odbcinst.ini, if any."""
    try:
        out = subprocess.run(["odbcinst", "-q", "-d", "-n", DRIVER],
                             capture_output=True, text=True, check=False).stdout
    except OSError:
        return None
    for line in out.splitlines():
        if line.startswith("Driver="):
            return line.split("=", 1)[1].strip()
    return None


def main() -> int:
    try:
        pyodbc.connect(f"DRIVER={{{DRIVER}}};Server=127.0.0.1,1", timeout=2)
    except pyodbc.Error as e:
        sqlstate = str(e.args[0]) if e.args else ""
        if sqlstate not in DRIVER_NOT_LOADED_SQLSTATES:
            print(f"OK: {DRIVER} loads (expected connect failure, SQLSTATE {sqlstate})")
            return 0
        # Safe to print: no credentials or row data are involved.
        print(f"FAIL: {DRIVER} could not be loaded: {e}", file=sys.stderr)
        path = _driver_path()
        if path is None:
            print(f"{DRIVER} is not registered in odbcinst.ini", file=sys.stderr)
        else:
            try:
                ldd = subprocess.run(["ldd", path], capture_output=True,
                                     text=True, check=False)
                print(f"ldd {path}:\n{ldd.stdout}{ldd.stderr}", file=sys.stderr)
            except OSError as ldd_error:
                print(f"ldd unavailable: {ldd_error}", file=sys.stderr)
        return 1
    print("FAIL: unexpectedly connected to 127.0.0.1,1", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
