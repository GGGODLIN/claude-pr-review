#!/usr/bin/env python3

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("pr-review-profile.py")

SAMPLE_YAML = """\
acme/saas:
  trunk: develop
  spec_globs:
    - docs/specs/*-design.md
    - docs/plans/*.md
  conventions_docs:
    - CLAUDE.md
    - docs/domain.md
acme/dashboard:
  trunk: develop
  spec_globs:
    - .scratch/*/spec.md
acme/no-trunk:
  spec_globs:
    - docs/*.md
"""


def run(remote, yaml_text):
  with tempfile.TemporaryDirectory() as tmp:
    yaml_path = Path(tmp) / "repos.yaml"
    yaml_path.write_text(yaml_text)
    proc = subprocess.run(
      [sys.executable, str(SCRIPT), "--remote", remote, "--profile", str(yaml_path)],
      capture_output=True,
      text=True,
    )
    return proc, str(yaml_path)


DEFAULT = {"trunk": None, "spec_globs": [], "conventions_docs": [], "source": "default"}


class ProfileResolverTest(unittest.TestCase):
  def test_https_remote_resolves_full_profile(self):
    proc, yaml_path = run("https://github.com/acme/saas.git", SAMPLE_YAML)
    self.assertEqual(proc.returncode, 0, proc.stderr)
    self.assertEqual(json.loads(proc.stdout), {
      "trunk": "develop",
      "spec_globs": ["docs/specs/*-design.md", "docs/plans/*.md"],
      "conventions_docs": ["CLAUDE.md", "docs/domain.md"],
      "source": yaml_path,
    })

  def test_optional_fields_default_to_empty(self):
    proc, yaml_path = run("https://github.com/acme/dashboard.git", SAMPLE_YAML)
    self.assertEqual(proc.returncode, 0, proc.stderr)
    self.assertEqual(json.loads(proc.stdout), {
      "trunk": "develop",
      "spec_globs": [".scratch/*/spec.md"],
      "conventions_docs": [],
      "source": yaml_path,
    })

  def test_unknown_remote_returns_default(self):
    proc, _ = run("https://github.com/acme/unknown.git", SAMPLE_YAML)
    self.assertEqual(proc.returncode, 0, proc.stderr)
    self.assertEqual(json.loads(proc.stdout), DEFAULT)

  def test_missing_trunk_returns_default(self):
    proc, _ = run("https://github.com/acme/no-trunk.git", SAMPLE_YAML)
    self.assertEqual(proc.returncode, 0, proc.stderr)
    self.assertEqual(json.loads(proc.stdout), DEFAULT)

  def test_ssh_and_https_resolve_same_key(self):
    ssh, _ = run("git@github.com:acme/saas.git", SAMPLE_YAML)
    https, _ = run("https://github.com/acme/saas", SAMPLE_YAML)
    self.assertEqual(ssh.returncode, 0, ssh.stderr)
    self.assertEqual(json.loads(ssh.stdout)["trunk"], "develop")
    self.assertEqual(json.loads(ssh.stdout)["trunk"], json.loads(https.stdout)["trunk"])

  def test_missing_profile_file_returns_default(self):
    proc = subprocess.run(
      [sys.executable, str(SCRIPT), "--remote", "https://github.com/acme/saas.git",
       "--profile", "/nonexistent/repos.yaml"],
      capture_output=True, text=True,
    )
    self.assertEqual(proc.returncode, 0, proc.stderr)
    self.assertEqual(json.loads(proc.stdout), DEFAULT)

  def test_empty_remote_returns_default(self):
    proc, _ = run("", SAMPLE_YAML)
    self.assertEqual(proc.returncode, 0, proc.stderr)
    self.assertEqual(json.loads(proc.stdout), DEFAULT)

  def test_invalid_yaml_exits_nonzero(self):
    proc, _ = run("https://github.com/acme/saas.git", "acme/saas: [unclosed\n  trunk: x\n")
    self.assertNotEqual(proc.returncode, 0)
    self.assertEqual(proc.stdout, "")
    self.assertIn("repos.yaml", proc.stderr)


if __name__ == "__main__":
  unittest.main()
