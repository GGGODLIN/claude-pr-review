#!/usr/bin/env python3

import argparse
import json
import re
import sys
from pathlib import Path

import yaml


REMOTE_KEY = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$")

DEFAULT = {"trunk": None, "spec_globs": [], "conventions_docs": [], "source": "default"}


def remote_to_key(remote):
  match = REMOTE_KEY.search(remote.strip())
  return f"{match.group(1)}/{match.group(2)}" if match else None


def as_list(value):
  if value is None:
    return []
  if isinstance(value, str):
    return [value]
  return [str(item) for item in value]


def load_profiles(path):
  profile_path = Path(path).expanduser()
  if not profile_path.is_file():
    return {}
  try:
    data = yaml.safe_load(profile_path.read_text()) or {}
  except yaml.YAMLError as error:
    raise SystemExit(f"repos.yaml parse error ({profile_path}): {error}")
  if not isinstance(data, dict):
    raise SystemExit(f"repos.yaml parse error ({profile_path}): top level must be a mapping")
  return data


def resolve(remote, profile_path):
  key = remote_to_key(remote or "")
  if key is None:
    return dict(DEFAULT)
  entry = load_profiles(profile_path).get(key)
  if not isinstance(entry, dict) or not entry.get("trunk"):
    return dict(DEFAULT)
  return {
    "trunk": str(entry["trunk"]),
    "spec_globs": as_list(entry.get("spec_globs")),
    "conventions_docs": as_list(entry.get("conventions_docs")),
    "source": str(Path(profile_path).expanduser()),
  }


def main(argv=None):
  parser = argparse.ArgumentParser(description="Resolve /pr-review repo profile from git remote URL")
  parser.add_argument("--remote", default="", help="git remote URL (https or SSH)")
  parser.add_argument("--profile", default="~/.claude/pr-review/repos.yaml", help="path to repos.yaml")
  args = parser.parse_args(argv)
  print(json.dumps(resolve(args.remote, args.profile), ensure_ascii=False))
  return 0


if __name__ == "__main__":
  sys.exit(main())
