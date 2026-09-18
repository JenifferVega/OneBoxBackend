#!/usr/bin/env python3
"""Runs the probe against the code AS DEPLOYED on 2026-09-16. No git writes.

    python scripts/probe_as_deployed.py gemini

`git stash` cannot be used here: a stale .git/index.lock makes git refuse to
write, and it fails QUIETLY -- a stash that saves nothing, followed by a probe
that claims to test the old code while testing the new one. That already
happened once; hence this script, which only copies files.

It swaps two files for their version at commit d7c5248 (the last commit before
the incident), runs the probe in a subprocess, and puts them back in a finally
block -- so an interrupt or a crash still restores the tree.

  agent/graph/nodes/planner/schemas.py   params: dict  (no declared properties)
  agent/graph/gemini_adapter.py          the sanitiser without the guard, which
                                         would otherwise raise on exactly the
                                         schema we are trying to observe
"""
import os
import shutil
import subprocess
import sys
import tempfile

REV = "d7c5248"
FILES = ["agent/graph/nodes/planner/schemas.py", "agent/graph/gemini_adapter.py"]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

provider = sys.argv[1] if len(sys.argv) > 1 else "gemini"
backup = tempfile.mkdtemp(prefix="onebox_current_")
swapped = []

try:
    for rel in FILES:
        old = subprocess.run(["git", "show", f"{REV}:{rel}"],
                             capture_output=True, text=True)
        if old.returncode != 0:
            sys.exit(f"cannot read {rel} at {REV}: {old.stderr.strip()}")
        keep = os.path.join(backup, rel.replace("/", "__"))
        shutil.copy2(rel, keep)
        with open(rel, "w", encoding="utf-8", newline="") as f:
            f.write(old.stdout)
        swapped.append((rel, keep))
        print(f"  swapped to {REV}: {rel}")

    # A subprocess, so the old modules are imported fresh.
    print(f"\n  running the probe on '{provider}' with the deployed code\n"
          f"  {'-' * 60}")
    subprocess.run([sys.executable, "scripts/probe_empty_params.py", provider])
finally:
    for rel, keep in swapped:
        shutil.copy2(keep, rel)
        print(f"  restored: {rel}")
    shutil.rmtree(backup, ignore_errors=True)
    print("\n  working tree is back to your current code "
          "(check with: git status --short)")
