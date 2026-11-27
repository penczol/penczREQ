"""Run the snapshot inspector from a source checkout."""
import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy.truenas.sqlite_snapshot import inspect_snapshot


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot_root", type=Path)
    print(json.dumps(inspect_snapshot(parser.parse_args().snapshot_root), indent=2))
