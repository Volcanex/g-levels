"""Run the repository's screen walk, retaining Wing's original harness as a fallback."""
import argparse
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    command = repo / ".glevels/screens"
    try:
        if command.is_file():
            body = command.read_text()
            if not any(line.strip() and not line.lstrip().startswith("#") for line in body.splitlines()):
                raise ValueError(".glevels/screens has no command")
            result = subprocess.run(["/bin/sh", str(command)], cwd=repo)
        elif (repo / "app/test/screens.py").is_file():
            result = subprocess.run(["/usr/bin/python3", "app/test/screens.py"], cwd=repo)
        else:
            raise ValueError("no .glevels/screens command or app/test/screens.py harness")
        return result.returncode
    except (OSError, ValueError) as error:
        print(f"impossible: {error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
