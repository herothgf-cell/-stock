import tempfile
from pathlib import Path
import unittest
from stockjournal.session import InstanceLock


class SessionTests(unittest.TestCase):
    def test_second_writer_rejected_and_lock_released(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'lock'
            lock=InstanceLock(path)
            try:
                with self.assertRaises(RuntimeError):
                    InstanceLock(path)
            finally:
                lock.close()
            another=InstanceLock(path);another.close()

    def test_independent_data_directories_can_run(self):
        with tempfile.TemporaryDirectory() as directory:
            first=InstanceLock(Path(directory)/'one'/'lock')
            second=InstanceLock(Path(directory)/'two'/'lock')
            second.close();first.close()
