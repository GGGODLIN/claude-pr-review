#!/usr/bin/env python3

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path


TARGETS_SCRIPT = Path(__file__).with_name("pr-review-targets.py")
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CC_INITIAL_ANGLES = {"context-aware primary", "security"}


def load_targets_module():
  spec = importlib.util.spec_from_file_location("pr_review_targets", TARGETS_SCRIPT)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


TARGETS = load_targets_module()


def selected_cells(selection):
  result = []
  seen = set()
  for cell in selection or []:
    if cell.get("status") not in (None, "selected"):
      continue
    seat = str(cell.get("seat") or "")
    angle = str(cell.get("angle") or seat)
    key = (seat, angle)
    if not seat or angle not in CC_INITIAL_ANGLES or key in seen:
      continue
    seen.add(key)
    result.append({**cell, "seat": seat, "angle": angle})
  return result


def target_material(target):
  return {
    "target_identity": target["target_identity"],
    "destination_repo": target.get("destination_repo"),
    "review_root": target.get("review_root"),
    "head": target.get("head"),
    "base": target.get("base"),
    "authored_diff_base": target.get("authored_diff_base"),
    "changed_files": list(target.get("changed_files") or []),
    "rules": dict(target.get("rules") or {}),
    "materials": dict(target.get("materials") or {}),
  }


def ownership_for(state, cell):
  rows = state.get("division_of_labor") or []
  row = next((item for item in rows if str(item.get("seat") or "") == cell["seat"]), None)
  if row is not None:
    return list(row.get("primary_material") or [])
  return [
    {"target_identity": target["target_identity"],
     "changed_files": list(target.get("changed_files") or [])}
    for target in state.get("targets") or []
  ]


def dispatch_id(cell, index):
  value = f"{index}\0{cell['seat']}\0{cell['angle']}"
  return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def plan_dispatch(state, selection, confirmation):
  targets = state.get("targets") or []
  if len(targets) == 1:
    return {
      "status": "SKIPPED",
      "mode": "legacy-single-target",
      "reason": "single-target-uses-existing-flow",
      "dispatches": [],
      "coverage": {"state": "LEGACY", "unverified": []},
    }
  decision = TARGETS.dispatch_decision(state, bool(confirmation))
  if not decision["allowed"]:
    return {
      "status": "BLOCKED",
      "mode": "multi-target",
      "reason": decision["reason"],
      "dispatches": [],
      "coverage": {"state": "UNVERIFIED", "unverified": []},
    }
  cells = selected_cells(selection)
  if not cells:
    return {
      "status": "SKIPPED",
      "mode": "multi-target",
      "reason": "cc-not-selected",
      "dispatches": [],
      "coverage": {"state": "UNVERIFIED", "unverified": []},
    }
  dispatches = []
  for index, cell in enumerate(cells, start=1):
    dispatches.append({
      "dispatch_id": dispatch_id(cell, index),
      "phase": "initial",
      "seat": cell["seat"],
      "angle": cell["angle"],
      "targets": [target_material(target) for target in targets],
      "file_ownership": ownership_for(state, cell),
      "extra_context": next((list(item.get("extra_context") or [])
                             for item in state.get("division_of_labor") or []
                             if str(item.get("seat") or "") == cell["seat"]), []),
    })
  return {
    "status": "READY",
    "mode": "multi-target",
    "reason": "confirmed",
    "dispatches": dispatches,
    "coverage": {"state": "PENDING", "unverified": []},
  }


def failed_entry(dispatch, status, error):
  return {
    "dispatch_id": dispatch["dispatch_id"],
    "seat": dispatch["seat"],
    "angle": dispatch["angle"],
    "phase": dispatch["phase"],
    "status": status,
    "error": str(error),
  }


def invoke_reviewer(reviewer, dispatch):
  try:
    response = reviewer(dispatch)
  except TimeoutError as error:
    return None, failed_entry(dispatch, "TIMEOUT", error)
  except Exception as error:
    return None, failed_entry(dispatch, "FAILED", error)
  if not isinstance(response, dict) or response.get("status") != "ok":
    reason = response.get("reason") if isinstance(response, dict) else "invalid-response"
    return None, failed_entry(dispatch, "FAILED", reason or "reviewer-failed")
  return response, None


def item_key(item):
  return str(item.get("target_identity") or ""), str(item.get("file") or "")


def interface_checks(interfaces, repair_response):
  returned = {
    str(item.get("interface_id") or ""): item
    for item in (repair_response or {}).get("checks") or []
    if item.get("interface_id")
  }
  result = []
  for interface in interfaces:
    interface_id = str(interface.get("interface_id") or "")
    check = returned.get(interface_id) or {}
    status = "completed" if check.get("status") == "completed" and check.get("evidence") else "unverified"
    result.append({
      **interface,
      "status": status,
      "evidence": str(check.get("evidence") or ""),
    })
  return result


def ledger_path(state):
  run_id = str(state.get("run_id") or "")
  preparation_path = state.get("preparation_path")
  if not preparation_path or not SAFE_RUN_ID.fullmatch(run_id) or ".." in run_id:
    return None
  root = Path(str(preparation_path)).expanduser().resolve(strict=False).parent
  path = root / "materials" / run_id / "cc-group-ledger.json"
  path.parent.mkdir(parents=True, exist_ok=True)
  return path


def write_ledger(state, result):
  path = ledger_path(state)
  if path is None:
    return None
  path.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True))
  return str(path)


def run_group_review(request, reviewer):
  state = request.get("state") or {}
  plan = plan_dispatch(state, request.get("selection") or [], request.get("confirmation"))
  if plan["status"] != "READY":
    return {
      **plan,
      "dispatch_ledger": [],
      "failed_seats": [],
      "new_interface_checks": [],
      "repair_rounds": 0,
      "group_payload": None,
      "reviewer_call_count": 0,
    }

  dispatch_ledger = []
  failed_seats = []
  findings = []
  missed_files = []
  new_interfaces = []
  reviewer_call_count = 0
  for dispatch in plan["dispatches"]:
    reviewer_call_count += 1
    response, failure = invoke_reviewer(reviewer, dispatch)
    if failure:
      failed_seats.append(failure)
      dispatch_ledger.append(failure)
      continue
    dispatch_ledger.append({
      "dispatch_id": dispatch["dispatch_id"],
      "seat": dispatch["seat"],
      "angle": dispatch["angle"],
      "phase": "initial",
      "status": "COMPLETED",
      "targets": dispatch["targets"],
      "file_ownership": dispatch["file_ownership"],
    })
    findings.extend(response.get("findings") or [])
    missed_files.extend(response.get("missed_files") or [])
    new_interfaces.extend(response.get("new_interfaces") or [])

  repair_response = None
  repair_rounds = 0
  repair_seat = str(request.get("repair_seat") or "")
  repair_source = next((item for item in plan["dispatches"] if item["seat"] == repair_seat), None)
  if (missed_files or new_interfaces) and repair_source is not None:
    repair_rounds = 1
    repair = {
      **repair_source,
      "dispatch_id": hashlib.sha256(
        f"repair\0{repair_source['dispatch_id']}".encode("utf-8")).hexdigest()[:16],
      "phase": "repair",
      "missed_files": missed_files,
      "new_interfaces": new_interfaces,
    }
    reviewer_call_count += 1
    repair_response, failure = invoke_reviewer(reviewer, repair)
    if failure:
      failed_seats.append(failure)
      dispatch_ledger.append(failure)
    else:
      findings.extend(repair_response.get("findings") or [])
      dispatch_ledger.append({
        "dispatch_id": repair["dispatch_id"],
        "seat": repair["seat"],
        "angle": repair["angle"],
        "phase": "repair",
        "status": "COMPLETED",
        "missed_files": missed_files,
        "new_interfaces": new_interfaces,
      })

  checks = interface_checks(new_interfaces, repair_response)
  accounted = {item_key(item) for item in (repair_response or {}).get("accounted_files") or []}
  unverified_files = [item for item in missed_files if item_key(item) not in accounted]
  coverage = {
    "state": "COMPLETE" if not unverified_files else "UNVERIFIED",
    "unverified": unverified_files,
  }
  group_payload = {
    "set_identity": state.get("set_identity"),
    "targets": [
      {"target_identity": target["target_identity"], "head": target.get("head"),
       "base": target.get("base"), "version_state": target.get("version_state") or "current"}
      for target in state.get("targets") or []
    ],
    "findings": findings,
  }
  result = {
    "status": "PARTIAL" if failed_seats or any(item["status"] == "unverified" for item in checks)
    or unverified_files else "COMPLETE",
    "mode": "multi-target",
    "dispatch_ledger": dispatch_ledger,
    "failed_seats": failed_seats,
    "new_interface_checks": checks,
    "repair_rounds": repair_rounds,
    "coverage": coverage,
    "group_payload": group_payload,
    "reviewer_call_count": reviewer_call_count,
  }
  result["ledger_path"] = write_ledger(state, result)
  return result


def load_state(payload):
  if isinstance(payload.get("state"), dict):
    return payload["state"]
  path = payload.get("preparation_path")
  if not path:
    return {}
  try:
    return json.loads(Path(str(path)).expanduser().read_text())
  except (OSError, ValueError, TypeError):
    return {}


def main(argv=None):
  parser = argparse.ArgumentParser()
  parser.add_argument("mode", choices=("plan", "reduce"))
  args = parser.parse_args(argv)
  try:
    payload = json.load(sys.stdin)
  except (json.JSONDecodeError, OSError, UnicodeDecodeError):
    json.dump({"status": "ERROR", "reason_codes": ["INVALID_INPUT_JSON"]}, sys.stdout)
    sys.stdout.write("\n")
    return 1
  state = load_state(payload)
  if args.mode == "plan":
    result = plan_dispatch(state, payload.get("selection") or state.get("selection") or [],
                           payload.get("confirmation"))
  else:
    responses = list(payload.get("responses") or [])
    def reviewer(_request):
      if not responses:
        return {"status": "error", "reason": "missing-fixed-response"}
      return responses.pop(0)
    result = run_group_review({**payload, "state": state}, reviewer)
  json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
  sys.stdout.write("\n")
  return 0


if __name__ == "__main__":
  sys.exit(main())
