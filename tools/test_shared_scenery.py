import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
SCRIPT = TOOLS / "check_shared_scenery.py"


def run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


class SharedSceneryCheckTests(unittest.TestCase):
    def test_missing_sentinels_report_and_fail(self):
        with tempfile.TemporaryDirectory() as td:
            scenery = Path(td) / "scenery"
            (scenery / "Models").mkdir(parents=True)
            proc = run("--scenery-root", str(scenery), "--json", "--fail-on-missing")
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            report = json.loads(proc.stdout)
            self.assertFalse(report["ok"])
            self.assertGreater(report["missing_count"], 0)
            self.assertIn("Models/Airport/Vehicle/Cobus_3000.xml", report["missing"])

    def test_complete_fixture_passes(self):
        with tempfile.TemporaryDirectory() as td:
            scenery = Path(td) / "scenery"
            # create all default sentinels and known deps
            files = {
                "Models/Airport/windsock_lit.xml": "<PropertyList/>",
                "Models/Industrial/generic_chimney_01.xml": "<PropertyList/>",
                "Models/Airport/Vehicle/Cobus_3000.xml": "<PropertyList><path>Cobus_3000.ac</path></PropertyList>",
                "Models/Airport/Vehicle/Cobus_3000.ac": 'AC3Db\ntexture "Cobus_3000.png"\n',
                "Models/Airport/Vehicle/Cobus_3000.png": "png",
                "Models/Industrial/GenericStorageTank40m.ac": "AC3Db\n",
                "Models/Boundaries/Fence_50m.ac": "AC3Db\n",
                "Models/Residential/french_house_s.xml": (
                    "<PropertyList><path>french_house_s.ac</path>"
                    "<texture>texture_french_house_s1.png</texture>"
                    "<texture>texture_french_house_s2.png</texture></PropertyList>"
                ),
                "Models/Residential/french_house_s.ac": 'AC3Db\ntexture "texture_french_house_s1.png"\n',
                "Models/Residential/texture_french_house_s1.png": "png1",
                "Models/Residential/texture_french_house_s2.png": "png2",
            }
            for rel, content in files.items():
                p = scenery / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
            proc = run("--scenery-root", str(scenery), "--json", "--fail-on-missing")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            report = json.loads(proc.stdout)
            self.assertTrue(report["ok"])
            self.assertEqual(report["missing_count"], 0)

    def test_default_mode_does_not_download_or_write_target(self):
        with tempfile.TemporaryDirectory() as td:
            scenery = Path(td) / "scenery"
            target = Path(td) / "target-root"
            (scenery / "Models").mkdir(parents=True)
            proc = run("--scenery-root", str(scenery), "--json")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertFalse(target.exists(), "default mode must not create/download target root")
            report = json.loads(proc.stdout)
            self.assertNotIn("download", report)

    def test_target_root_must_not_be_existing_scenery_root(self):
        with tempfile.TemporaryDirectory() as td:
            scenery = Path(td) / "scenery"
            (scenery / "Models").mkdir(parents=True)
            proc = run(
                "--scenery-root", str(scenery),
                "--download-missing-official",
                "--target-root", str(scenery),
                "--json",
            )
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn("must not be an existing", proc.stderr)


if __name__ == "__main__":
    unittest.main()
