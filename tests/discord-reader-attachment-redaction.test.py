"""A top-level attachment's FILENAME is user-supplied, so it goes through _redact().

The forward branch composes filenames into `inner` and redacts that; the
ordinary-message branch returned its marks straight to the caller, so a secret
pasted into a filename reached the reader by one branch and not the other.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from channels.discord.reader import _render  # noqa: E402

TOKEN = "ghp_" + "A" * 36
CDN = "https://cdn.discordapp.com/attachments/1/2/x"


class TopLevelAttachmentMarksAreRedacted(unittest.TestCase):
    def test_a_secret_in_a_filename_does_not_reach_the_reader(self):
        out = _render({"content": "", "attachments": [
            {"filename": f"vault set X {TOKEN}.txt", "url": CDN}]}, clip=None)
        self.assertNotIn(TOKEN, out)

    def test_the_forward_branch_still_redacts(self):
        out = _render({"content": "", "message_snapshots": [{"message": {
            "content": "", "attachments": [
                {"filename": f"vault set X {TOKEN}.txt", "url": CDN}]}}]}, clip=None)
        self.assertNotIn(TOKEN, out)

    def test_an_ordinary_attachment_keeps_its_name_and_url(self):
        # The redaction must not cost the retrievable handle the PR exists to carry.
        out = _render({"content": "", "attachments": [
            {"filename": "notes.pdf", "url": CDN}]}, clip=None)
        self.assertIn("notes.pdf", out)
        self.assertIn(CDN, out)


if __name__ == "__main__":
    unittest.main()
