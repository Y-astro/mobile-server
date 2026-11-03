import glob
import io
import os
import signal
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import MagicMock, patch

from document_conversion import DEFAULT_CONVERSION_ROOT, convert_documents_batch


class DocumentConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.conversion_root = self.temp_dir.name

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_empty_file_list(self):
        out_bytes, out_name, mime, err = convert_documents_batch([], conversion_root=self.conversion_root)
        self.assertIsNone(out_bytes)
        self.assertIsNone(out_name)
        self.assertIsNone(mime)
        self.assertEqual(err, "No files provided")

    @patch("subprocess.Popen")
    def test_single_file_success(self, mock_popen):
        batch_dir_captured = []

        def fake_communicate(timeout=None):
            # simulate proot/libreoffice creating doc_000.pdf in batch_dir
            batch_dirs = glob.glob(os.path.join(self.conversion_root, "batch_*"))
            self.assertTrue(len(batch_dirs) == 1)
            bdir = batch_dirs[0]
            batch_dir_captured.append(bdir)
            pdf_path = os.path.join(bdir, "doc_000.pdf")
            with open(pdf_path, "wb") as f:
                f.write(b"%PDF-1.4 simulated pdf")
            return "", ""

        proc = MagicMock()
        proc.communicate.side_effect = fake_communicate
        proc.returncode = 0
        mock_popen.return_value = proc

        files = [("report.docx", b"dummy docx bytes")]
        out_bytes, out_name, mime, err = convert_documents_batch(files, conversion_root=self.conversion_root)

        self.assertIsNone(err)
        self.assertEqual(out_name, "report.pdf")
        self.assertEqual(mime, "application/pdf")
        self.assertEqual(out_bytes, b"%PDF-1.4 simulated pdf")

        # Verify Popen call arguments and configuration
        mock_popen.assert_called_once()
        call_args, call_kwargs = mock_popen.call_args
        cmd = call_args[0]
        bdir = batch_dir_captured[0]
        expected_cmd = [
            "proot-distro", "login", "debian", "--",
            "libreoffice", f"-env:UserInstallation=file://{bdir}/profile", "--headless", "--convert-to", "pdf",
            os.path.join(bdir, "doc_000.docx"),
            "--outdir", bdir,
        ]
        self.assertEqual(cmd, expected_cmd)
        self.assertEqual(call_kwargs.get("stdout"), subprocess.PIPE)
        self.assertEqual(call_kwargs.get("stderr"), subprocess.PIPE)
        self.assertTrue(call_kwargs.get("text"))
        self.assertTrue(call_kwargs.get("start_new_session"))

        # Verify communicate was called with timeout 47 (35 + 1 * 12)
        proc.communicate.assert_called_once_with(timeout=47)

        # Ensure temporary batch directory is removed
        self.assertEqual(glob.glob(os.path.join(self.conversion_root, "batch_*")), [])

    @patch("subprocess.Popen")
    def test_multiple_files_zip_success(self, mock_popen):
        batch_dir_captured = []

        def fake_communicate(timeout=None):
            batch_dirs = glob.glob(os.path.join(self.conversion_root, "batch_*"))
            bdir = batch_dirs[0]
            batch_dir_captured.append(bdir)
            with open(os.path.join(bdir, "doc_000.pdf"), "wb") as f:
                f.write(b"PDF 0")
            with open(os.path.join(bdir, "doc_001.pdf"), "wb") as f:
                f.write(b"PDF 1")
            return "", ""

        proc = MagicMock()
        proc.communicate.side_effect = fake_communicate
        proc.returncode = 0
        mock_popen.return_value = proc

        files = [("file1.docx", b"bytes1"), ("file2.txt", b"bytes2")]
        out_bytes, out_name, mime, err = convert_documents_batch(files, conversion_root=self.conversion_root)

        self.assertIsNone(err)
        self.assertEqual(out_name, "converted_documents.zip")
        self.assertEqual(mime, "application/zip")
        self.assertIsNotNone(out_bytes)

        # Verify Popen call arguments and configuration
        mock_popen.assert_called_once()
        call_args, call_kwargs = mock_popen.call_args
        cmd = call_args[0]
        bdir = batch_dir_captured[0]
        expected_cmd = [
            "proot-distro", "login", "debian", "--",
            "libreoffice", f"-env:UserInstallation=file://{bdir}/profile", "--headless", "--convert-to", "pdf",
            os.path.join(bdir, "doc_000.docx"),
            os.path.join(bdir, "doc_001.txt"),
            "--outdir", bdir,
        ]
        self.assertEqual(cmd, expected_cmd)
        self.assertEqual(call_kwargs.get("stdout"), subprocess.PIPE)
        self.assertEqual(call_kwargs.get("stderr"), subprocess.PIPE)
        self.assertTrue(call_kwargs.get("text"))
        self.assertTrue(call_kwargs.get("start_new_session"))

        # Verify communicate was called with timeout 59 (35 + 2 * 12)
        proc.communicate.assert_called_once_with(timeout=59)

        with zipfile.ZipFile(io.BytesIO(out_bytes), "r") as zf:
            namelist = zf.namelist()
            self.assertIn("file1.pdf", namelist)
            self.assertIn("file2.pdf", namelist)
            self.assertEqual(zf.read("file1.pdf"), b"PDF 0")
            self.assertEqual(zf.read("file2.pdf"), b"PDF 1")

        self.assertEqual(glob.glob(os.path.join(self.conversion_root, "batch_*")), [])

    @patch("subprocess.Popen")
    def test_duplicate_filenames_suffix_policy(self, mock_popen):
        def fake_communicate(timeout=None):
            batch_dirs = glob.glob(os.path.join(self.conversion_root, "batch_*"))
            bdir = batch_dirs[0]
            with open(os.path.join(bdir, "doc_000.pdf"), "wb") as f:
                f.write(b"PDF 0")
            with open(os.path.join(bdir, "doc_001.pdf"), "wb") as f:
                f.write(b"PDF 1")
            with open(os.path.join(bdir, "doc_002.pdf"), "wb") as f:
                f.write(b"PDF 2")
            return "", ""

        proc = MagicMock()
        proc.communicate.side_effect = fake_communicate
        proc.returncode = 0
        mock_popen.return_value = proc

        files = [("doc.docx", b"b1"), ("doc.docx", b"b2"), ("doc.docx", b"b3")]
        out_bytes, out_name, mime, err = convert_documents_batch(files, conversion_root=self.conversion_root)

        self.assertIsNone(err)
        self.assertEqual(out_name, "converted_documents.zip")

        with zipfile.ZipFile(io.BytesIO(out_bytes), "r") as zf:
            namelist = zf.namelist()
            self.assertEqual(sorted(namelist), ["doc.pdf", "doc_2.pdf", "doc_3.pdf"])

    @patch("subprocess.Popen")
    def test_no_output_error(self, mock_popen):
        proc = MagicMock()
        proc.communicate.return_value = ("", "LibreOffice crash dump")
        proc.returncode = 1
        mock_popen.return_value = proc

        files = [("broken.docx", b"bad bytes")]
        out_bytes, out_name, mime, err = convert_documents_batch(files, conversion_root=self.conversion_root)

        self.assertIsNone(out_bytes)
        self.assertIn("Batch conversion error: LibreOffice crash dump", err)
        self.assertEqual(glob.glob(os.path.join(self.conversion_root, "batch_*")), [])

    @patch("os.killpg")
    @patch("subprocess.Popen")
    def test_timeout_cleanup_and_killpg(self, mock_popen, mock_killpg):
        proc = MagicMock()
        proc.pid = 99999
        proc.communicate.side_effect = [subprocess.TimeoutExpired(cmd="fake", timeout=35), ("", "")]
        mock_popen.return_value = proc

        files = [("large.docx", b"large data")]
        out_bytes, out_name, mime, err = convert_documents_batch(files, conversion_root=self.conversion_root)

        self.assertIsNone(out_bytes)
        self.assertEqual(err, "Conversion timed out for 1 files")
        mock_killpg.assert_called_once_with(99999, signal.SIGKILL)
        self.assertEqual(glob.glob(os.path.join(self.conversion_root, "batch_*")), [])


if __name__ == "__main__":
    unittest.main()
