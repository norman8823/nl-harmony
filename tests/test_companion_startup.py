"""An occupied port reports the original problem without failing during cleanup."""
import contextlib
import errno
import io
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

from harmony import cli


class CompanionStartupTests(unittest.TestCase):
    def test_bind_failure_preserves_oserror_without_initializing_quota(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            server = cli._load_board_server()
            with mock.patch.object(server, "default_quota") as quota:
                with self.assertRaises(OSError) as caught:
                    server.build_server(Path(directory), Path(directory) / ".workflow",
                                        port=listener.getsockname()[1], session="test", actor="test")
                self.assertEqual(caught.exception.errno, errno.EADDRINUSE)
                quota.assert_not_called()

    def test_cli_reports_how_to_restart_instead_of_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                result = cli.main(["--project", directory, "companion", "--no-browser", "--port", str(port)])
            self.assertEqual(result, 2)
            self.assertIn(f"port {port} is already in use", err.getvalue())
            self.assertIn("Ctrl+C", err.getvalue())
            self.assertIn("same board directory", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
