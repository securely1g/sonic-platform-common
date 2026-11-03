"""Reject misleading wheel inventories in incremental build evidence."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile

from wheel_incremental import ORIGINAL, SOURCE, WHEEL, check, payload, payload_delta


class PayloadEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.before = {name: {"sha256": "before", "mode": 0o100644 << 16} for name in
                       (SOURCE, "sonic_platform_common-1.0.dist-info/RECORD", "sonic_platform_base/device_base.py")}
        self.after = copy.deepcopy(self.before)
        for name in (SOURCE, "sonic_platform_common-1.0.dist-info/RECORD"):
            self.after[name]["sha256"] = "after"

    def test_expected_edit_preserves_other_payloads(self):
        self.assertEqual(len(payload_delta(self.before, self.after)), 2)

    def test_edited_file_cannot_change_mode(self):
        self.after[SOURCE]["mode"] = 0o100755 << 16
        with self.assertRaisesRegex(AssertionError, "modes"):
            payload_delta(self.before, self.after)

    def test_unrelated_file_cannot_change(self):
        self.after["sonic_platform_base/device_base.py"]["sha256"] = "after"
        with self.assertRaisesRegex(AssertionError, "unrelated"):
            payload_delta(self.before, self.after)

    def test_duplicate_zip_members_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.whl"
            with zipfile.ZipFile(path, "w") as archive, warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(SOURCE, "first")
                archive.writestr(SOURCE, "second")
            with self.assertRaisesRegex(AssertionError, "Duplicate"):
                payload(path)


class CheckoutTrustTest(unittest.TestCase):
    def test_different_owner_checkout_preserves_source_guard_and_restoration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "checkout"
            root.mkdir()
            source = root / SOURCE
            source.parent.mkdir()
            source.write_bytes(ORIGINAL + b"\n")
            for command in (["init", "-q"], ["add", SOURCE],
                            ["-c", "user.name=Wheel test", "-c", "user.email=test@example.invalid",
                             "commit", "-qm", "Fixture"]):
                subprocess.run(["git", *command], cwd=root, check=True)

            def wheel_phase(repository, output, bazel, phase, expected):
                directory = output / phase
                directory.mkdir(parents=True)
                wheel = directory / WHEEL
                with zipfile.ZipFile(wheel, "w") as archive:
                    for name, value in ((SOURCE, source.read_bytes()),
                                        ("sonic_platform_common-1.0.dist-info/RECORD", source.read_bytes()),
                                        ("sonic_platform_base/device_base.py", b"unchanged")):
                        archive.writestr(zipfile.ZipInfo(name, date_time=(2023, 1, 1, 0, 0, 0)), value)
                return {"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "executed_actions": 0}

            environment = {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1",
                           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
            with mock.patch.dict(os.environ, environment), mock.patch(
                    "wheel_incremental.build", side_effect=wheel_phase) as build:
                rejected = subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", SOURCE],
                                          cwd=root, capture_output=True, text=True)
                self.assertNotEqual(rejected.returncode, 0, "negative control must reject untrusted checkout")
                self.assertIn("Could not access", rejected.stderr)
                output = Path(temporary) / "evidence"
                check(root, output, "unused")
                self.assertEqual(build.call_count, 4)
                self.assertEqual(source.read_bytes(), ORIGINAL + b"\n")
                receipt = json.loads((output / "receipt.json").read_text())
                self.assertEqual(receipt["status"], "passed")
                self.assertTrue(receipt["source_restored"])
                # Trusting this exact checkout must not bypass the clean-source fence.
                source.write_bytes(ORIGINAL + b"\n# unexpected change\n")
                build.reset_mock()
                with self.assertRaises(subprocess.CalledProcessError):
                    check(root, Path(temporary) / "dirty-evidence", "unused")
                build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
