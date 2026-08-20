import importlib.util
from pathlib import Path
import sys
import unittest

spec = importlib.util.spec_from_file_location("models_under_test", Path("endstone_bot/models.py"))
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
FakePlayer = module.FakePlayer


class ModelTests(unittest.TestCase):
    def test_ai_fields_round_trip(self):
        fp = FakePlayer(id="x", name="Bot", ai_enabled=True, ai_members=["Alex"])
        restored = FakePlayer.from_record(fp.to_record())
        self.assertTrue(restored.ai_enabled)
        self.assertEqual(restored.ai_members, ["Alex"])

    def test_old_record_defaults(self):
        fp = FakePlayer.from_record({"id": "x", "name": "Old"})
        self.assertFalse(fp.ai_enabled)
        self.assertEqual(fp.ai_members, [])


if __name__ == "__main__":
    unittest.main()
