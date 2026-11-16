from pathlib import Path
import runpy
import unittest


release_notes = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / ".github" / "scripts" / "release_notes.py")
)["release_notes"]


class ReleaseNotesTests(unittest.TestCase):
    def test_extracts_the_exact_tag_section(self) -> None:
        changelog = (
            "## [Unreleased]\n\nPending changes\n"
            "## [v0.3.0] - 2026-11-22\n\n- Released changes\n"
            "## [v0.2.0]\n\n- Earlier changes\n"
        )
        self.assertEqual(release_notes(changelog, "v0.3.0"), "- Released changes\n")

    def test_rejects_missing_empty_and_duplicate_sections(self) -> None:
        for changelog in ("## [v0.3.1]\n\n- Other", "## [v0.3.0]\n\n",
                          "## [v0.3.0]\n- One\n## [v0.3.0]\n- Two"):
            with self.subTest(changelog=changelog), self.assertRaises(ValueError):
                release_notes(changelog, "v0.3.0")
