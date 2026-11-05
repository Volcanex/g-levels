import os
import tempfile

import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        if ({"browser_available", "render", "renders"} & set(item.fixturenames)
            or item.originalname.startswith("test_browser_")
            or item.originalname in {"test_real_browser_walk_evidence", "test_rendered_url_uses_real_chromium",
                                     "test_spacing_blocks_the_failure_fixtures"}):
            item.add_marker(pytest.mark.browser)


@pytest.fixture(autouse=True, scope="session")
def contained_tmp(tmp_path_factory):
    # Sensors default their output to mkdtemp, and one suite run leaves gigabytes of renders.
    # Point them into pytest's own temp tree, which keeps only the last three runs.
    root = tmp_path_factory.mktemp("sensor-tmp")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TMPDIR", str(root))
        # Keep directories under var/ from discovering the enclosing checkout.
        mp.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path_factory.getbasetemp()), prepend=os.pathsep)
        mp.setattr(tempfile, "tempdir", None)
        yield root
