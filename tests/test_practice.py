import importlib.util
from pathlib import Path
import sys
import unittest

spec = importlib.util.spec_from_file_location("models_practice", Path("endstone_bot/models.py"))
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
FakePlayer = module.FakePlayer


class PracticeTests(unittest.TestCase):
    def test_private_profile_round_trip(self):
        fp = FakePlayer(
            id="p", name="PlayerBot", practice_managed=True,
            practice_follow=True, practice_random_move=True,
            practice_slow_falling=True, practice_fire_resistance=True,
            practice_infinite_totem=True, practice_armor="netherite",
        )
        restored = FakePlayer.from_record(fp.to_record())
        self.assertTrue(restored.practice_managed)
        self.assertTrue(restored.practice_follow)
        self.assertTrue(restored.practice_random_move)
        self.assertTrue(restored.practice_slow_falling)
        self.assertTrue(restored.practice_fire_resistance)
        self.assertTrue(restored.practice_infinite_totem)
        self.assertEqual(restored.practice_armor, "netherite")

    def test_invalid_armor_defaults_to_none(self):
        fp = FakePlayer.from_record({"practiceArmor": "creative"})
        self.assertEqual(fp.practice_armor, "none")


if __name__ == "__main__":
    unittest.main()
