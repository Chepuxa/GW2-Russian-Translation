from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "tools"))
import release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_integrity_failure_is_detected(self):
        file=self.root / "game/version.dll"
        file.parent.mkdir(parents=True)
        file.write_bytes(b"known test bytes")
        entry={"path":"game/version.dll","size":file.stat().st_size,"sha256":release.sha256(file)}
        release.verify_assets(self.root,[entry])
        file.write_bytes(b"changed test bytes")
        with self.assertRaisesRegex(ValueError,"integrity"):
            release.verify_assets(self.root,[entry])

    def test_paths_cannot_escape_package(self):
        for path in ("../other.dll","game/../../other.dll","/outside.dll","C:/outside.dll",r"..\other.dll"):
            with self.subTest(path=path),self.assertRaises(ValueError):
                release.safe_file(self.root,path)

    def test_duplicate_assets_are_rejected(self):
        file=self.root / "one"
        file.write_bytes(b"x")
        entry={"path":"one","size":1,"sha256":release.sha256(file)}
        with self.assertRaisesRegex(ValueError,"Duplicate"):
            release.verify_assets(self.root,[entry,entry])


if __name__ == "__main__":
    unittest.main()
