import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.packaging
def test_wheel_excludes_tests(tmp_path):
    dist = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--out-dir", str(dist)],
        cwd=ROOT,
        check=True,
    )

    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1

    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        test_paths = [name for name in names if "/tests/" in name]

    assert test_paths == []
    assert "gdrive_fsspec/__init__.py" in names
    assert "gdrive_fsspec/core.py" in names
