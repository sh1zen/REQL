from pathlib import Path
import unittest


class ReleaseWorkflowTests(unittest.TestCase):
    def test_workflows_leave_pypi_upload_to_the_maintainer(self) -> None:
        workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        self.assertTrue((workflows / "release.yml").is_file())

        for workflow in workflows.iterdir():
            if workflow.suffix in {".yml", ".yaml"}:
                with self.subTest(workflow=workflow.name):
                    source = workflow.read_text(encoding="utf-8")
                    self.assertNotIn("gh-action-pypi-publish", source)
                    self.assertNotIn("twine upload", source)

    def test_release_publishes_notes_without_python_distributions(self) -> None:
        workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
        source = workflow.read_text(encoding="utf-8")

        self.assertIn("path: release-notes.md", source)
        self.assertIn('gh release create "$RELEASE_TAG" --verify-tag', source)
        self.assertNotIn("python -m build", source)
        self.assertNotIn("reql-release-assets", source)
