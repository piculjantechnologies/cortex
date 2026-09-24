"""Importing the pipeline has no side effects; every entry point has a working --help."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("torch")
pytest.importorskip("torchvision")
pytest.importorskip("cv2")
pytest.importorskip("bs4")
pytest.importorskip("azure.storage.blob")
pytest.importorskip("url_normalize")
pytest.importorskip("filetype")

pytestmark = pytest.mark.pipeline

BACKEND = Path(__file__).resolve().parents[2]
ENTRY_POINTS = ["download_commoncrawl", "process_commoncrawl", "detect_objects", "estimate_label_quality",
                "backfill_object_stats"]
LABELQA = ["pipeline.labelqa.base", "pipeline.labelqa.unified.voc"] + [
    f"pipeline.labelqa.{model}.{part}" for model in ("thesis", "paper", "unified") for part in ("network", "preprocessing")]
MODULES = (["pipeline", "pipeline.config", "pipeline.db", "pipeline.http", "pipeline.labelqa", "pipeline.object_stats"]
           + LABELQA + [f"pipeline.{m}" for m in ENTRY_POINTS])


def run_python(args, tmp_path):
    env = {k: v for k, v in os.environ.items()
           if k not in ("MONGO_URI", "mongo_db_uri", "AZURE_STORAGE_CONNECTION_STRING", "connect_str")}
    env["TORCH_HOME"] = str(tmp_path / "torch-home")
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")  # where the label-quality checkpoint would be downloaded
    return subprocess.run([sys.executable, *args], cwd=BACKEND, env=env, capture_output=True,
                          text=True, timeout=120)


def test_import_has_no_side_effects_and_no_flask(tmp_path):
    code = (
        "import sys\n"
        f"for name in {MODULES!r}:\n"
        "    __import__(name)\n"
        "import pipeline.db, pipeline.http\n"
        "assert pipeline.db._client is None, 'MongoClient created at import'\n"
        "assert pipeline.http._default is None, 'Fetcher created at import'\n"
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] in ('flask', 'app', 'flask_sqlalchemy'))\n"
        "assert not leaked, leaked\n"
        "print('ok')\n"
    )
    result = run_python(["-c", code], tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    # no model weights were downloaded or loaded
    assert not (tmp_path / "torch-home").exists() or not any((tmp_path / "torch-home").rglob("*.pth"))
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("module", ENTRY_POINTS)
def test_entry_point_help(module, tmp_path):
    result = run_python(["-m", f"pipeline.{module}", "--help"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage:")
    assert not (tmp_path / "cache").exists()
