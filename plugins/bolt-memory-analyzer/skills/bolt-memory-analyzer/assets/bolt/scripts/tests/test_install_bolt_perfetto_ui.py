import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "install_bolt_perfetto_ui",
    SCRIPTS_DIR / "install_bolt_perfetto_ui.py",
)
installer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = installer
spec.loader.exec_module(installer)


class PerfettoUiInstallerTest(unittest.TestCase):
    def test_registers_plugin_id_once(self):
        content = "export const defaultPlugins = [\n  'existing.Plugin',\n];\n"
        updated = installer.update_default_plugins(content)
        updated_twice = installer.update_default_plugins(updated)

        self.assertIn("'dev.bolt.Memory'", updated)
        self.assertEqual(updated, updated_twice)
        self.assertEqual(updated.count("'dev.bolt.Memory'"), 1)

    def test_plans_all_plugin_files(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "dev.bolt.Memory"
            files = installer.planned_files(
                installer.PLUGIN_SOURCE,
                destination,
            )

        names = {source.name for source, _ in files}
        self.assertEqual(names, {"index.ts", "sql.ts"})


if __name__ == "__main__":
    unittest.main()
