import tempfile

import pytest


@pytest.fixture(autouse=True, scope="session")
def contained_tmp(tmp_path_factory):
    # Sensors default their output to mkdtemp, and one suite run leaves gigabytes of renders.
    # Point them into pytest's own temp tree, which keeps only the last three runs.
    root = tmp_path_factory.mktemp("sensor-tmp")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TMPDIR", str(root))
        mp.setattr(tempfile, "tempdir", None)
        yield root
