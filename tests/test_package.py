import unittest

from ntulearn_skill import __version__


class PackageTest(unittest.TestCase):
    def test_phase_zero_version(self) -> None:
        self.assertEqual(__version__, "0.0.0")


if __name__ == "__main__":
    unittest.main()
