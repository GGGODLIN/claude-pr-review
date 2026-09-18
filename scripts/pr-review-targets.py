#!/usr/bin/env python3

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path


PROFILE_SCRIPT = Path(__file__).with_name("pr-review-profile.py")
GITHUB_URL = re.compile(r"^https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)$")
BITBUCKET_URL = re.compile(r"^https?://bitbucket\.org/([^/\s]+)/([^/\s]+)/pull-requests/(\d+)$")
SCP_REMOTE = re.compile(r"^(?:[^/@:]+@)?([^/:]+):(.+)$")
BARE_NUMBER = re.compile(r"^\d+$")
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
SAFE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
LOCKFILES = (
  "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
  "gemfile.lock", "poetry.lock", "pipfile.lock", "cargo.lock", "go.sum", "composer.lock",
)
GENERATED_SUFFIXES = (".min.js", ".min.css", ".map", ".generated.ts")
VENDORED_SEGMENTS = ("dist/", "build/", "vendor/", "node_modules/", "coverage/")
DOC_SUFFIXES = (".md", ".mdx", ".rst", ".txt", ".adoc")
FILE_COUNT_THRESHOLD = 15
DIFF_LINES_THRESHOLD = 800
REVIEW_SET_CAPACITY_KEY = "review_set"
MATERIALS_DIRNAME = "materials"


def load_profile_module():
  spec = importlib.util.spec_from_file_location("pr_review_profile", PROFILE_SCRIPT)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


PROFILE = load_profile_module()


def sha256_text(value):
  return hashlib.sha256(value.encode("utf-8")).hexdigest()


def git(checkout, *args):
  return subprocess.run(
    ["git", "-C", str(checkout), *args],
    capture_output=True,
    text=True,
  )


def git_ok(checkout, *args):
  proc = git(checkout, *args)
  return proc.stdout.strip() if proc.returncode == 0 else None


def remote_identity(remote):
  remote = (remote or "").strip()
  if not remote:
    return None
  if "://" in remote:
    parts = urllib.parse.urlsplit(remote)
    host, path = (parts.hostname or "").lower(), parts.path
  else:
    match = SCP_REMOTE.match(remote)
    if not match:
      return None
    host, path = match.group(1).lower(), match.group(2)
  segments = [segment for segment in path.strip("/").split("/") if segment]
  if len(segments) < 2 or "." not in host:
    return None
  return host, f"{segments[-2]}/{re.sub(r'\.git$', '', segments[-1])}"


def platform_for_host(host):
  if host == "github.com" or host.endswith(".github.com"):
    return "github"
  if host == "bitbucket.org" or host.endswith(".bitbucket.org"):
    return "bitbucket"
  return host


def parse_input(item, current_repo):
  raw = str(item.get("raw") or "").strip()
  for pattern, host in ((GITHUB_URL, "github.com"), (BITBUCKET_URL, "bitbucket.org")):
    match = pattern.match(raw)
    if match:
      owner, repo, pr = match.groups()
      return {
        "host": host,
        "platform": platform_for_host(host),
        "destination_repo": f"{owner}/{repo}",
        "pr": pr,
      }, None
  if BARE_NUMBER.match(raw):
    if not item.get("named_in_conversation") or not current_repo:
      return None, "AMBIGUOUS_INPUT"
    identity = remote_identity(current_repo.get("remote"))
    if identity is None:
      return None, "AMBIGUOUS_INPUT"
    host, repo = identity
    return {
      "host": host,
      "platform": platform_for_host(host),
      "destination_repo": repo,
      "pr": raw,
    }, None
  return None, "AMBIGUOUS_INPUT"


def target_identity(parsed):
  return f"{parsed['platform']}:{parsed['host']}/{parsed['destination_repo']}#{parsed['pr']}"


def platform_key(parsed):
  return f"{parsed['host']}/{parsed['destination_repo']}#{parsed['pr']}"


def normalize_inputs(payload):
  reason_codes = []

  def note(code):
    if code not in reason_codes:
      reason_codes.append(code)

  items = payload.get("inputs")
  if not items:
    note("EMPTY_INPUT")
    return [], reason_codes
  current_repo = payload.get("current_repo")
  parsed_items = []
  seen = set()
  for item in items:
    parsed, reason = parse_input(item, current_repo)
    if reason:
      note(reason)
      continue
    declared_repo = item.get("declared_repo")
    declared_pr = item.get("declared_pr")
    if ((declared_repo is not None and str(declared_repo) != parsed["destination_repo"])
        or (declared_pr is not None and str(declared_pr) != parsed["pr"])):
      note("CONTRADICTORY_INPUT")
      continue
    identity = target_identity(parsed)
    if identity in seen:
      continue
    seen.add(identity)
    parsed_items.append(parsed)
  parsed_items.sort(key=target_identity)
  if not parsed_items and not reason_codes:
    note("EMPTY_INPUT")
  return parsed_items, reason_codes


def source_files(changed_files):
  result = []
  for value in changed_files or []:
    path = str(value)
    lowered = path.lower()
    if lowered.rsplit("/", 1)[-1] in LOCKFILES:
      continue
    if lowered.endswith(GENERATED_SUFFIXES):
      continue
    if any(segment in lowered for segment in VENDORED_SEGMENTS):
      continue
    if lowered.endswith(DOC_SUFFIXES):
      continue
    result.append(path)
  return result


def target_slug(target):
  repo = target["destination_repo"].replace("/", "-")
  return f"{target['platform']}-{target['host']}-{repo}-{target['pr']}"


def review_root_for(checkout, target, run_id, total):
  worktrees = checkout / ".worktrees"
  if total == 1:
    return worktrees / f"review-pr-{target['pr']}"
  return worktrees / f"review-pr-{run_id}" / target_slug(target)


def materials_root_for(state_root, target, run_id, total):
  base = Path(str(state_root)).expanduser() / MATERIALS_DIRNAME / run_id
  if total == 1:
    return base / f"review-pr-{target['pr']}"
  return base / target_slug(target)


def safe_slug(value):
  return bool(SAFE_SLUG.match(str(value or ""))) and ".." not in str(value)


def adjudicate_target(payload, parsed, response, repos):
  identity = target_identity(parsed)

  def failure(reason_code, detail):
    return None, {"target_identity": identity, "pr": parsed["pr"],
                  "reason_code": reason_code, "detail": detail}

  if not isinstance(response, dict) or response.get("status") != "ok":
    detail = response.get("reason") if isinstance(response, dict) else None
    return failure("TARGET_FETCH_FAILED", f"platform fetch failed ({detail})" if detail else "platform fetch failed")
  if str(response.get("destination_repo")) != parsed["destination_repo"]:
    return failure("TARGET_IDENTITY_MISMATCH",
                   f"platform reports destination {response.get('destination_repo')}")

  repo_entry = (repos or {}).get(response.get("destination_repo")) or {}
  checkout_value = repo_entry.get("checkout")
  if not checkout_value:
    return failure("CHECKOUT_MISSING", "no checkout configured for destination repo")
  checkout = Path(str(checkout_value)).expanduser()
  if not checkout.is_dir():
    return failure("CHECKOUT_MISSING", f"checkout not found: {checkout}")

  remote = repo_entry.get("remote") or git_ok(checkout, "remote", "get-url", "origin")
  remote_here = remote_identity(remote)
  if remote_here is None:
    return failure("CHECKOUT_IDENTITY_MISMATCH", f"checkout remote is not a resolvable remote: {remote}")
  if remote_here != (parsed["host"], parsed["destination_repo"]):
    return failure("CHECKOUT_IDENTITY_MISMATCH",
                   f"checkout remote resolves to {remote_here[0]}/{remote_here[1]}, "
                   f"target is {parsed['host']}/{parsed['destination_repo']}")

  head = str(response.get("head") or "")
  base = str(response.get("base") or "")
  if not COMMIT_SHA.match(head) or not COMMIT_SHA.match(base):
    return failure("TARGET_VERSION_MISSING", "head/base must be exact commit SHAs, not branch names")
  for label, ref, sha in (
    ("head", str(response.get("head_ref") or ""), head),
    ("base", str(response.get("base_ref") or ""), base),
  ):
    if not ref:
      return failure("TARGET_VERSION_UNRESOLVED", f"{label} ref missing from platform response")
    resolved = git_ok(checkout, "rev-parse", f"refs/remotes/origin/{ref}")
    if resolved is None:
      return failure("TARGET_VERSION_UNRESOLVED", f"origin/{ref} not present in checkout")
    if resolved != sha:
      return failure("TARGET_VERSION_MISMATCH", f"origin/{ref} is {resolved}, platform reports {sha}")
    if git_ok(checkout, "cat-file", "-e", f"{sha}^{{commit}}") is None:
      return failure("TARGET_VERSION_UNRESOLVED", f"{sha} is not a commit in the checkout")

  profile = PROFILE.resolve(remote, payload.get("profile_path") or "")
  trunk = profile["trunk"]
  trunk_source = f"profile {profile['source']}" if trunk else None
  if not trunk:
    symbolic = git_ok(checkout, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if symbolic:
      trunk = symbolic.split("/", 1)[1] if "/" in symbolic else symbolic
      trunk_source = "origin/HEAD"
  if not trunk:
    trunk, trunk_source = "master", "default master"

  authored_diff_base = git_ok(checkout, "merge-base", f"refs/remotes/origin/{trunk}", head) or base
  return {
    "target_identity": identity,
    "platform": parsed["platform"],
    "host": parsed["host"],
    "destination_repo": parsed["destination_repo"],
    "pr": parsed["pr"],
    "source_repo": str(response.get("source_repo") or parsed["destination_repo"]),
    "head": head,
    "head_ref": str(response.get("head_ref") or ""),
    "base": base,
    "base_ref": str(response.get("base_ref") or ""),
    "trunk": trunk,
    "trunk_source": trunk_source,
    "authored_diff_base": authored_diff_base,
    "checkout": str(checkout),
    "changed_files": [str(path) for path in response.get("changed_files") or []],
    "diff_lines": int(response.get("diff_lines") or 0),
    "specs": [
      {"path": str(spec.get("path")), "content": str(spec.get("content") or "")}
      for spec in response.get("specs") or []
    ],
    "rules": {
      "trunk": trunk,
      "spec_globs": list(profile["spec_globs"]),
      "conventions_docs": list(profile["conventions_docs"]),
      "source": profile["source"],
    },
  }, None


def materialize_target(materials_root, target):
  materials_root.mkdir(parents=True, exist_ok=True)
  entries = []
  for index, spec in enumerate(target["specs"]):
    digest = sha256_text(spec["content"])
    spec_path = materials_root / f"spec-{index + 1}-{Path(spec['path']).name}"
    spec_path.write_text(spec["content"])
    entries.append({
      "path": spec["path"],
      "file": str(spec_path),
      "content": spec["content"],
      "content_sha256": digest,
      "bytes": len(spec["content"].encode("utf-8")),
    })
  return {"root": str(materials_root), "specs": entries}


def build_manifest(targets):
  shared = {}
  per_target = {}
  for target in targets:
    entries = []
    for spec in target["materials"]["specs"]:
      shared_key = f"sha256:{spec['content_sha256']}"
      bucket = shared.setdefault(shared_key, {
        "key": shared_key,
        "content": spec["content"],
        "content_sha256": spec["content_sha256"],
        "bytes": spec["bytes"],
        "source_targets": [],
      })
      if target["target_identity"] not in bucket["source_targets"]:
        bucket["source_targets"].append(target["target_identity"])
        bucket["source_targets"].sort()
      entries.append({
        "path": spec["path"],
        "file": spec["file"],
        "content_sha256": spec["content_sha256"],
        "bytes": spec["bytes"],
        "shared_key": bucket["key"],
      })
    per_target[target["target_identity"]] = {
      "source_repo": target["source_repo"],
      "head": target["head"],
      "base": target["base"],
      "specs": entries,
      "rules": target["rules"],
      "changed_files": target["changed_files"],
      "transported_bytes": sum(entry["bytes"] for entry in entries),
    }
  return {"shared": shared, "targets": per_target}


def capacity_report(targets):
  per_target = {}
  gapped = []
  for target in targets:
    file_count = len(source_files(target["changed_files"]))
    diff_lines = target["diff_lines"]
    chunked = file_count > FILE_COUNT_THRESHOLD or diff_lines > DIFF_LINES_THRESHOLD
    per_target[target["target_identity"]] = {
      "source_file_count": file_count,
      "diff_lines": diff_lines,
      "chunked": chunked,
      "chunk_count": (file_count + FILE_COUNT_THRESHOLD - 1) // FILE_COUNT_THRESHOLD if chunked else 0,
      "threshold": {"source_files": FILE_COUNT_THRESHOLD, "diff_lines": DIFF_LINES_THRESHOLD},
    }
    if chunked:
      gapped.append(target["target_identity"])
  return per_target, gapped


def aggregate_capacity_report(targets):
  file_count = sum(len(source_files(target["changed_files"])) for target in targets)
  diff_lines = sum(target["diff_lines"] for target in targets)
  chunked = file_count > FILE_COUNT_THRESHOLD or diff_lines > DIFF_LINES_THRESHOLD
  return {
    "source_file_count": file_count,
    "diff_lines": diff_lines,
    "chunked": chunked,
    "chunk_count": (file_count + FILE_COUNT_THRESHOLD - 1) // FILE_COUNT_THRESHOLD if chunked else 0,
    "target_count": len(targets),
    "threshold": {"source_files": FILE_COUNT_THRESHOLD, "diff_lines": DIFF_LINES_THRESHOLD},
  }


def division_of_labor(selection, targets, manifest):
  shared_bytes = sum(entry["bytes"] for entry in manifest["shared"].values())
  extra = [
    {"kind": "shared-material", "content_sha256": entry["content_sha256"],
     "bytes": entry["bytes"], "source_targets": entry["source_targets"]}
    for entry in manifest["shared"].values()
  ]
  conventions = {}
  for target in targets:
    for path in target["rules"]["conventions_docs"]:
      key = f"{target['destination_repo']}:{path}"
      bucket = conventions.setdefault(key, {
        "kind": "conventions", "path": path, "source_targets": [],
      })
      if target["target_identity"] not in bucket["source_targets"]:
        bucket["source_targets"].append(target["target_identity"])
        bucket["source_targets"].sort()
  extra.extend(conventions.values())
  rows = []
  seen = set()
  for cell in selection or []:
    seat = str(cell.get("seat") or "")
    if not seat or seat in seen:
      continue
    seen.add(seat)
    rows.append({
      "seat": seat,
      "angle": str(cell.get("angle") or seat),
      "primary_material": [
        {"target_identity": target["target_identity"], "changed_files": target["changed_files"]}
        for target in targets
      ],
      "extra_context": list(extra),
      "shared_bytes": shared_bytes,
    })
  return rows


def set_identity_of(targets):
  canonical = "\n".join(sorted(target["target_identity"] for target in targets))
  return f"sha256:{sha256_text(canonical)}"


def state_path_for(state_root, run_id):
  if not safe_slug(run_id):
    return None
  return Path(str(state_root)).expanduser() / f"{run_id}.json"


def write_state(path, value):
  path = Path(str(path)).expanduser()
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True))
  return str(path)


def read_state(path):
  if not path:
    return None
  try:
    return json.loads(Path(str(path)).expanduser().read_text())
  except (OSError, ValueError, TypeError):
    return None


def dispatch_decision(state, confirmed):
  if state.get("cancelled"):
    return {"allowed": False, "reason": "cancelled"}
  if state.get("status") != "READY":
    return {"allowed": False, "reason": "preparation-blocked"}
  if not state.get("selection_count"):
    return {"allowed": False, "reason": "selection-required"}
  if state.get("capacity", {}).get("adjudication_required"):
    return {"allowed": False, "reason": "awaiting-adjudication"}
  if not confirmed:
    return {"allowed": False, "reason": "awaiting-confirmation"}
  return {"allowed": True, "reason": "confirmed"}


def apply_selection(state, selection, adjudication):
  selection = selection or []
  adjudication = adjudication or {}
  seats = sorted({str(cell.get("seat")) for cell in selection if cell.get("seat")})
  targets = state.get("targets") or []
  per_target, gapped = capacity_report(targets)
  review_set = aggregate_capacity_report(targets)
  unresolved = [identity for identity in gapped if identity not in adjudication]
  if review_set["chunked"] and REVIEW_SET_CAPACITY_KEY not in adjudication:
    unresolved.append(REVIEW_SET_CAPACITY_KEY)
  if not gapped and not review_set["chunked"]:
    decision = "within-threshold"
  elif unresolved:
    decision = "awaiting-adjudication"
  else:
    decision = "adjudicated"
  state["selection"] = selection
  state["selection_count"] = 1 if selection else 0
  state["dispatch_seats"] = seats
  state["division_of_labor"] = division_of_labor(selection, targets, state["material_manifest"])
  state["capacity"] = {
    "decision": decision,
    "adjudication_required": bool(unresolved),
    "targets": per_target,
    "review_set": review_set,
    "unresolved": unresolved,
    "waiting_seats": seats if unresolved else [],
    "adjudication": adjudication,
  }
  state["dispatch"] = dispatch_decision(state, confirmed=False)
  return state


def prepare(payload):
  run_id = str(payload.get("run_id") or "")
  parsed_items, reason_codes = normalize_inputs(payload)
  state_path = state_path_for(payload.get("state_root"), run_id)
  result = {
    "status": "BLOCKED",
    "run_id": run_id,
    "targets": [],
    "failures": [],
    "reason_codes": list(reason_codes),
    "set_identity": None,
    "selection": [],
    "selection_count": 0,
    "dispatch_seats": [],
    "division_of_labor": [],
    "material_manifest": {"shared": {}, "targets": {}},
    "capacity": {
      "decision": "not-evaluated",
      "adjudication_required": False,
      "targets": {},
      "review_set": aggregate_capacity_report([]),
      "unresolved": [],
      "waiting_seats": [],
      "adjudication": {},
    },
    "dispatch": {"allowed": False, "reason": "preparation-blocked"},
    "cancelled": False,
  }
  if state_path is None:
    result["reason_codes"] = sorted(set([*reason_codes, "INVALID_RUN_ID"]))
    result["preparation_path"] = None
    return result
  result["preparation_path"] = str(state_path)

  if reason_codes:
    result["status"] = "NEEDS_INPUT"
    result["dispatch"] = {"allowed": False, "reason": "awaiting-correction"}
    write_state(state_path, result)
    return result

  platform = payload.get("platform") or {}
  for parsed in parsed_items:
    response = platform.get(platform_key(parsed)) or {"status": "error", "reason": "no_platform_response"}
    target, failure = adjudicate_target(payload, parsed, response, payload.get("repos"))
    if failure:
      result["failures"].append(failure)
    else:
      result["targets"].append(target)
  if result["failures"]:
    result["targets"] = []
    write_state(state_path, result)
    return result

  total = len(result["targets"])
  for target in result["targets"]:
    target["review_root"] = str(review_root_for(Path(target["checkout"]), target, run_id, total))
    target["materials"] = materialize_target(
      materials_root_for(payload.get("state_root"), target, run_id, total), target,
    )

  result["status"] = "READY"
  result["set_identity"] = set_identity_of(result["targets"])
  result["material_manifest"] = build_manifest(result["targets"])
  result = apply_selection(result, [], {})
  write_state(state_path, result)
  return result


def finalize(payload):
  path = payload.get("preparation_path")
  state = read_state(path)
  if state is None:
    return {"status": "ERROR", "reason_codes": ["PREPARATION_NOT_FOUND"]}
  if state.get("status") != "READY":
    return {
      "status": "ERROR",
      "reason_codes": ["PREPARATION_NOT_READY"],
      "state_status": state.get("status"),
      "preparation_path": str(path),
    }
  selection = payload.get("selection")
  if selection is None:
    selection = state.get("selection") or []
  state = apply_selection(state, selection, payload.get("adjudication") or {})
  state["preparation_path"] = str(path)
  write_state(path, state)
  return state


def authorize_dispatch(payload):
  state = read_state(payload.get("preparation_path"))
  if state is None:
    return {"allowed": False, "reason": "preparation-not-found"}
  return dispatch_decision(state, bool(payload.get("confirmation")))


def cancel(payload):
  requested = payload.get("preparation_path")
  if not requested:
    return {"cancelled": False, "removed": [], "left_to_step7": [],
            "reason_codes": ["PREPARATION_NOT_FOUND"]}
  try:
    state_file = Path(str(requested)).expanduser().resolve(strict=True)
  except (OSError, RuntimeError):
    return {"cancelled": False, "removed": [], "left_to_step7": [],
            "reason_codes": ["PREPARATION_NOT_FOUND"]}
  state = read_state(state_file)
  if state is None or not state_file.is_file():
    return {"cancelled": False, "removed": [], "left_to_step7": [],
            "reason_codes": ["PREPARATION_NOT_FOUND"]}
  run_id = str(state.get("run_id") or "")
  if not safe_slug(run_id) or state_file.parent / f"{run_id}.json" != state_file:
    return {"cancelled": False, "removed": [], "left_to_step7": [],
            "reason_codes": ["STATE_RUN_ID_MISMATCH"]}

  materials_base = (state_file.parent / MATERIALS_DIRNAME / run_id)
  try:
    materials_base = materials_base.resolve(strict=False)
  except (OSError, RuntimeError):
    return {"cancelled": False, "removed": [], "left_to_step7": [],
            "reason_codes": ["STATE_RUN_ID_MISMATCH"]}

  planned = []
  for target in state.get("targets") or []:
    recorded = (target.get("materials") or {}).get("root")
    if not recorded:
      continue
    try:
      root = Path(str(recorded)).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
      return {"cancelled": False, "removed": [], "left_to_step7": [],
              "reason_codes": ["STATE_MATERIALS_OUT_OF_SCOPE"]}
    if root == materials_base or not root.is_relative_to(materials_base):
      return {"cancelled": False, "removed": [], "left_to_step7": [],
              "reason_codes": ["STATE_MATERIALS_OUT_OF_SCOPE"]}
    planned.append((str(recorded), root))

  left_to_step7 = [target.get("review_root") for target in state.get("targets") or []
                   if target.get("review_root")]
  removed = []
  for recorded, root in planned:
    if root.is_dir():
      shutil.rmtree(root, ignore_errors=True)
    if not root.exists():
      removed.append(recorded)
  if materials_base.is_dir() and not any(materials_base.iterdir()):
    materials_base.rmdir()
  state["status"] = "CANCELLED"
  state["cancelled"] = True
  state["targets"] = []
  state["division_of_labor"] = []
  state["material_manifest"] = {"shared": {}, "targets": {}}
  state["capacity"]["review_set"] = aggregate_capacity_report([])
  state["capacity"]["unresolved"] = []
  state["capacity"]["waiting_seats"] = []
  state["dispatch"] = {"allowed": False, "reason": "cancelled"}
  write_state(state_file, state)
  return {
    "cancelled": True,
    "removed": removed,
    "left_to_step7": left_to_step7,
    "reason_codes": [],
  }


def main(argv=None):
  parser = argparse.ArgumentParser()
  parser.add_argument("mode", choices=("prepare", "finalize", "cancel", "authorize-dispatch"))
  args = parser.parse_args(argv)
  try:
    payload = json.load(sys.stdin)
  except (json.JSONDecodeError, OSError, UnicodeDecodeError):
    json.dump({"status": "ERROR", "reason_codes": ["INVALID_INPUT_JSON"]}, sys.stdout)
    sys.stdout.write("\n")
    return 1
  if args.mode == "prepare":
    result = prepare(payload)
  elif args.mode == "finalize":
    result = finalize(payload)
  elif args.mode == "cancel":
    result = cancel(payload)
  else:
    result = authorize_dispatch(payload)
  json.dump(result, sys.stdout, ensure_ascii=False)
  sys.stdout.write("\n")
  return 0 if result.get("status") != "ERROR" else 1


if __name__ == "__main__":
  sys.exit(main())
