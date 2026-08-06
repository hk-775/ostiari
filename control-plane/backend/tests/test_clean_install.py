"""A clean install must actually be clean.

`make clean-start` and `OSTIARI_NO_DEMO=1` promise an empty control plane. Two
things broke that promise: the Makefile deleted a `state.json` path the code had
stopped writing, and `seed_models()` ran at *import* time rather than from the
gated seeder block in `app.py`, so the 18 model configs appeared regardless.

The import-time seed is what makes this awkward to test in-process: the gate is
evaluated once, when the module first loads, and by the time a test runs the
module is already imported. So these tests run a real subprocess with the env var
set, which is also how an operator triggers it.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BACKEND = REPO / "control-plane" / "backend"


def _model_count(no_demo: bool) -> int:
    """Import the router in a fresh interpreter and count seeded models."""
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    if no_demo:
        env["OSTIARI_NO_DEMO"] = "1"
    else:
        env.pop("OSTIARI_NO_DEMO", None)
    out = subprocess.run(
        [sys.executable, "-c",
         "from control_plane.routers.model_config import _models;"
         "print(sum(len(v) for v in _models.values()))"],
        capture_output=True, text=True, env=env, cwd=str(BACKEND), timeout=120,
    )
    assert out.returncode == 0, out.stderr
    return int(out.stdout.strip().splitlines()[-1])


class TestModelSeedGating:
    def test_no_demo_seeds_nothing(self):
        assert _model_count(no_demo=True) == 0

    def test_default_still_seeds(self):
        """The demo must keep working — gating it off entirely would empty the
        Models page for everyone, which is not what a clean install asked for."""
        assert _model_count(no_demo=False) == 18

    def test_seed_models_is_still_callable_by_hand(self):
        """The escape hatch the docstring points operators at."""
        from control_plane.routers.model_config import _models, seed_models

        _models.clear()
        seed_models()
        assert sum(len(v) for v in _models.values()) == 18


class TestCleanStartWipesTheLivePath:
    def test_makefile_removes_the_path_persistence_writes(self):
        """The recipe deleted `control-plane/backend/data/state.json`, the path
        from before `env.data_dir()` — so a clean start restored the old stores."""
        from control_plane.persistence import STATE_FILE

        recipe = _clean_start_recipe()
        live = STATE_FILE.relative_to(REPO).as_posix()
        assert live in recipe, f"clean-start does not remove {live}"

    def test_state_file_is_under_the_shared_data_dir(self):
        """Guards the reason the paths diverged: two callers deriving the data dir
        from __file__ with a different number of .parent hops."""
        from control_plane.env import data_dir
        from control_plane.persistence import STATE_FILE

        assert STATE_FILE.parent == data_dir()

    def test_db_and_state_land_in_the_same_directory(self):
        from control_plane.env import data_dir, default_sqlite_url
        from control_plane.persistence import STATE_FILE

        assert Path(default_sqlite_url().split("///")[-1]).parent == STATE_FILE.parent == data_dir()


def _clean_start_recipe() -> str:
    """The body of the Makefile's clean-start target."""
    text = (REPO / "Makefile").read_text()
    m = re.search(r"^clean-start:.*?\n((?:\t.*\n)+)", text, re.MULTILINE)
    assert m, "clean-start target not found in Makefile"
    return m.group(1)
