"""Isolate the test database from the live trading DB."""
import os
import tempfile

os.environ["OMNI_DB"] = os.path.join(tempfile.mkdtemp(prefix="omni_test_"),
                                     "test.db")
