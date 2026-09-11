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
    ssh, _ = run("git@github.com:acme/saas.git", SAMPLE_YAML)
    self.assertEqual(json.loads(ssh.stdout)["trunk"], "develop")

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

  def test_scalar_list_field_is_a_clear_error_not_a_traceback(self):
    proc, _ = run("https://github.com/acme/saas.git", "acme/saas:\n  trunk: develop\n  spec_globs: 5\n")
    self.assertNotEqual(proc.returncode, 0)
    self.assertEqual(proc.stdout, "")
    self.assertIn("spec_globs", proc.stderr)
    self.assertNotIn("Traceback", proc.stderr)

  def test_local_path_remote_never_matches_a_profile(self):
    proc, _ = run("/Users/me/acme/saas", SAMPLE_YAML)
    self.assertEqual(proc.returncode, 0, proc.stderr)
    self.assertEqual(json.loads(proc.stdout), DEFAULT)

  def test_host_is_not_mistaken_for_owner(self):
    proc, _ = run("https://github.com/saas", SAMPLE_YAML)
    self.assertEqual(json.loads(proc.stdout), DEFAULT)


if __name__ == "__main__":
  unittest.main()
