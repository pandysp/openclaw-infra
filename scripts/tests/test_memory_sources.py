#!/usr/bin/env python3
"""Run the memory-sources copy job against fixture workspaces.

The job runs in a container in production; here only its extraction tools are
replaced, so the selection, copy, removal and failure rules run for real.
"""
import importlib.util
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / 'ansible/roles/memory-sources/files/memory-sources.py'


class MemorySourcesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memory-sources-')
        root = Path(self.tmp.name)
        self.source, self.out = root / 'workspace', root / 'out'
        self.source.mkdir()
        self.out.mkdir()
        spec = importlib.util.spec_from_file_location('memory_sources', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.module.SOURCE, self.module.OUT = self.source, self.out
        self.module.MANIFEST = self.out / '.manifest.json'
        self.extractions = []

        def fake_pdf(path):
            self.extractions.append(path.relative_to(self.source).as_posix())
            content = path.read_text()
            if content.startswith('broken'):
                raise RuntimeError('pdftotext exited 1')
            if content.startswith('crash'):
                raise KeyboardInterrupt('stands in for the kernel killing the container')
            if content.startswith('vanish'):
                path.unlink()
                raise FileNotFoundError(path)
            return 'PDF TEXT ' + content
        self.module.EXTRACT['pdf'] = fake_pdf

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, relative, content='content'):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def run_job(self):
        output = io.StringIO()
        with mock.patch.object(self.module.shutil, 'which', return_value='/usr/bin/tool'), redirect_stdout(output):
            code = self.module.main()
        return code, output.getvalue()

    def copies(self):
        return sorted(p.relative_to(self.out).as_posix() for p in self.out.rglob('*.md'))

    def test_copies_exactly_what_qmd_indexed_that_memory_search_cannot_read(self):
        self.write('notes/a.txt', 'OTTER')
        self.write('notes/deep/b.csv', 'x,LYNX')
        self.write('notes/c.md')  # memory search reads Markdown itself
        self.write('x.txt')  # qmd indexed text only under notes/
        self.write('docs/report.pdf', 'ZEBRA')  # extracted anywhere outside dot folders
        self.write('.scripts/hidden.pdf')
        self.write('notes/.obsidian/n.txt')
        (self.source / 'notes/link.txt').symlink_to('/etc/hosts')
        code, output = self.run_job()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.copies(), ['docs/report.pdf.md', 'notes/a.txt.md', 'notes/deep/b.csv.md'])
        copy = (self.out / 'notes/a.txt.md').read_text()
        self.assertTrue(copy.startswith('# notes/a.txt\n'))
        self.assertIn('OTTER', copy)
        self.assertIn('PDF TEXT ZEBRA', (self.out / 'docs/report.pdf.md').read_text())

    def test_unchanged_files_are_not_read_again(self):
        self.write('docs/report.pdf', 'ZEBRA')
        self.run_job()
        self.run_job()
        self.assertEqual(self.extractions, ['docs/report.pdf'])

    def test_deleted_and_renamed_originals_leave_no_copies(self):
        self.write('notes/old.txt', 'OLD')
        self.write('docs/gone/report.pdf', 'ZEBRA')
        self.run_job()
        (self.source / 'notes/old.txt').rename(self.source / 'notes/new.txt')
        (self.source / 'docs/gone/report.pdf').unlink()
        code, output = self.run_job()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.copies(), ['notes/new.txt.md'])
        self.assertFalse((self.out / 'docs').exists(), 'empty copy folders stay behind')

    def test_changed_originals_replace_their_copies(self):
        path = self.write('notes/a.txt', 'FIRST')
        self.run_job()
        path.write_text('SECOND VERSION')
        os.utime(path, ns=(1, 1))
        self.run_job()
        copy = (self.out / 'notes/a.txt.md').read_text()
        self.assertIn('SECOND VERSION', copy)
        self.assertNotIn('FIRST', copy)

    def test_a_file_that_cannot_be_read_fails_once_and_never_leaves_a_stale_copy(self):
        path = self.write('docs/report.pdf', 'ZEBRA')
        self.write('notes/a.txt', 'OTTER')
        self.run_job()
        path.write_text('broken now')
        code, output = self.run_job()
        self.assertEqual(code, 3, 'a newly unreadable file must fail the run so the owner is alerted')
        self.assertIn('UNREADABLE docs/report.pdf', output)
        self.assertEqual(self.copies(), ['notes/a.txt.md'], 'the old text must not stay searchable')
        # Not retried (or re-alerted) until it changes, but still reported.
        code, output = self.run_job()
        self.assertEqual(code, 0)
        self.assertIn('UNREADABLE docs/report.pdf', output)
        self.assertEqual(self.extractions.count('docs/report.pdf'), 2)
        path.write_text('readable again')
        os.utime(path, ns=(2, 2))
        code, output = self.run_job()
        self.assertEqual(code, 0)
        self.assertNotIn('UNREADABLE', output)
        self.assertIn('readable again', (self.out / 'docs/report.pdf.md').read_text())
        manifest = json.loads((self.out / '.manifest.json').read_text())
        self.assertEqual(manifest['unreadable'], {})

    def test_a_run_killed_on_one_file_keeps_its_progress_and_gives_up_after_three_stops(self):
        self.write('docs/a.pdf', 'ALPHA')
        self.write('docs/b.pdf', 'crash')
        self.write('docs/c.pdf', 'GAMMA')
        # A single stop may be a reboot: the file is read again, up to three times.
        for _ in range(3):
            with self.assertRaises(KeyboardInterrupt):
                self.run_job()
        self.assertEqual(self.extractions.count('docs/b.pdf'), 3)
        self.assertEqual(self.extractions.count('docs/a.pdf'), 1, 'progress before the crash was lost')
        code, output = self.run_job()
        self.assertEqual(code, 3, 'giving up on a file must alert the owner')
        self.assertIn('UNREADABLE docs/b.pdf: runs stopped while reading', output)
        self.assertEqual(self.copies(), ['docs/a.pdf.md', 'docs/c.pdf.md'])
        code, output = self.run_job()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.extractions.count('docs/b.pdf'), 3)

    def test_a_failure_that_may_pass_is_retried_before_the_owner_is_told(self):
        import subprocess
        def slow(path):
            self.extractions.append('slow')
            raise subprocess.TimeoutExpired('tesseract', 60)
        self.module.EXTRACT['image'] = slow
        self.write('docs/scan.png')
        for attempt in (1, 2):
            code, output = self.run_job()
            self.assertEqual(code, 0, output)
            self.assertIn(f'RETRY ({attempt}/3) docs/scan.png', output)
        code, output = self.run_job()
        self.assertEqual(code, 3)
        self.assertIn('UNREADABLE docs/scan.png: TimeoutExpired', output)
        self.run_job()
        self.assertEqual(self.extractions.count('slow'), 3)

    def test_a_file_deleted_while_being_read_is_not_reported(self):
        self.write('docs/a.pdf', 'vanish')
        code, output = self.run_job()
        self.assertEqual(code, 0, output)
        self.assertNotIn('UNREADABLE', output)
        self.assertEqual(self.copies(), [])

    def test_a_name_too_long_for_its_copy_is_reported_not_fatal(self):
        self.write('notes/' + 'n' * 250 + '.txt', 'LONG')
        self.write('notes/ok.txt', 'OK')
        code, output = self.run_job()
        self.assertEqual(code, 3)
        self.assertIn('UNREADABLE notes/nnn', output)
        self.assertEqual(self.copies(), ['notes/ok.txt.md'])

    def test_file_names_that_are_not_utf8_are_shown_readably(self):
        self.assertEqual(self.module.shown('notes/caf\udce9.txt'), 'notes/caf\ufffd.txt')

    def test_a_missing_extraction_tool_fails_the_run(self):
        with mock.patch.object(self.module.shutil, 'which', return_value=None):
            with self.assertRaises(SystemExit):
                self.module.main()


if __name__ == '__main__':
    unittest.main()
