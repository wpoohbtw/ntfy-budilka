"""Install requirements only when the pinned input changes."""

import hashlib
from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parent
    requirements = root / "requirements.txt"
    stamp = Path(sys.prefix) / ".requirements.sha256"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    if not stamp.exists() or stamp.read_text() != digest:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
            check=False,
        )
        if result.returncode:
            return result.returncode
        stamp.write_text(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
