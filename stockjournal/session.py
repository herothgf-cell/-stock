"""Hold an OS lock for the app lifetime: one writer per data directory."""
import os
from pathlib import Path


class InstanceLock:
    def __init__(self, path):
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open('a+b')
        try:
            # Windows byte-range locks forbid reads of the locked byte too.
            # Query file metadata so a second instance reaches the lock attempt.
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b'0'); self.file.flush()
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('같은 기록 폴더를 사용하는 프로그램이 이미 실행 중입니다.') from None

    def close(self):
        if self.file.closed:
            return
        if os.name == 'nt':
            import msvcrt
            self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()
