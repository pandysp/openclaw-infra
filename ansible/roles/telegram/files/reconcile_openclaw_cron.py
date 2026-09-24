#!/usr/bin/env python3
"""Reconcile OpenClaw cron jobs without replacing stable job IDs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


class ReconcileError(RuntimeError):
    pass


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ReconcileError(f"{label} must be a boolean")
    return value


def prepare_manifest(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str]]:
    if not isinstance(manifest, dict):
        raise ReconcileError("manifest root must be an object")
    default_enabled = _require_bool(
        manifest.get("scheduledAutomationDefault", False),
        "scheduledAutomationDefault",
    )
    agents = manifest.get("agents")
    jobs = manifest.get("jobs")
    if not isinstance(agents, list) or not isinstance(jobs, list):
        raise ReconcileError("manifest agents and jobs must be arrays")

    policies: dict[str, bool] = {}
    for entry in agents:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            raise ReconcileError("every agent needs a non-empty string id")
        agent_id = entry["id"]
        if agent_id in policies:
            raise ReconcileError(f"duplicate agent id: {agent_id}")
        policies[agent_id] = _require_bool(
            entry.get("scheduledAutomationEnabled", default_enabled),
            f"agent {agent_id} scheduledAutomationEnabled",
        )

    desired: list[dict[str, Any]] = []
    names: set[str] = set()
    for entry in jobs:
        if not isinstance(entry, dict):
            raise ReconcileError("every job must be an object")
        name = entry.get("name")
        agent_id = entry.get("agentId")
        if not isinstance(name, str) or not name:
            raise ReconcileError("every job needs a non-empty string name")
        if name in names:
            raise ReconcileError(f"duplicate desired cron name: {name}")
        names.add(name)
        if agent_id not in policies:
            raise ReconcileError(f"cron job {name} references unknown agent {agent_id}")

        state = entry.get("state", "present")
        if state not in ("present", "absent"):
            raise ReconcileError(f"cron job {name} has invalid state {state}")
        prepared = {"name": name, "agentId": agent_id, "state": state}
        if state == "absent":
            desired.append(prepared)
            continue

        own_enabled = _require_bool(entry.get("enabled", True), f"cron job {name} enabled")
        schedule = entry.get("schedule")
        if not isinstance(schedule, str) or len(schedule.split()) not in (5, 6):
            raise ReconcileError(f"cron job {name} needs a 5- or 6-field cron schedule")
        for field in ("timezone", "message", "deliveryChannel", "deliveryTo"):
            if not isinstance(entry.get(field), str) or not entry[field]:
                raise ReconcileError(f"cron job {name} needs a non-empty {field}")

        prepared.update(
            {
                "schedule": schedule,
                "timezone": entry["timezone"],
                "message": entry["message"],
                "deliveryChannel": entry["deliveryChannel"],
                "deliveryTo": entry["deliveryTo"],
                "enabled": policies[agent_id] and own_enabled,
            }
        )
        desired.append(prepared)

    paused_agents = {agent_id for agent_id, enabled in policies.items() if not enabled}
    return desired, paused_agents


def _owned_current(job: dict[str, Any]) -> dict[str, Any]:
    name = job.get("name", "<unnamed>")
    schedule = job.get("schedule")
    payload = job.get("payload")
    delivery = job.get("delivery")
    for label, value in (
        ("schedule", schedule),
        ("payload", payload),
        ("delivery", delivery),
    ):
        if not isinstance(value, dict):
            raise ReconcileError(f"live cron job {name} has a non-object {label}")
    return {
        "name": job.get("name"),
        "agentId": job.get("agentId"),
        "sessionTarget": job.get("sessionTarget"),
        "wakeMode": job.get("wakeMode"),
        "schedule": {
            "kind": schedule.get("kind"),
            "expr": schedule.get("expr"),
            "tz": schedule.get("tz"),
        },
        "payload": {"kind": payload.get("kind"), "message": payload.get("message")},
        "delivery": {
            "mode": delivery.get("mode"),
            "channel": delivery.get("channel"),
            "to": delivery.get("to"),
        },
    }


def _owned_desired(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": job["name"],
        "agentId": job["agentId"],
        "sessionTarget": "isolated",
        "wakeMode": "now",
        "schedule": {"kind": "cron", "expr": job["schedule"], "tz": job["timezone"]},
        "payload": {"kind": "agentTurn", "message": job["message"]},
        "delivery": {
            "mode": "announce",
            "channel": job["deliveryChannel"],
            "to": job["deliveryTo"],
        },
    }


def build_plan(manifest: dict[str, Any], current_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(current_jobs, list):
        raise ReconcileError("cron list jobs must be an array")
    desired, paused_agents = prepare_manifest(manifest)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for current in current_jobs:
        if not isinstance(current, dict):
            raise ReconcileError("cron list returned a non-object job")
        for field in ("id", "name", "agentId"):
            if not isinstance(current.get(field), str) or not current[field]:
                raise ReconcileError(f"live cron job has an invalid {field}")
        _require_bool(current.get("enabled"), f"live cron job {current['name']} enabled")
        by_name.setdefault(current["name"], []).append(current)

    actions: list[dict[str, Any]] = []
    desired_names = {job["name"] for job in desired}
    for job in desired:
        matches = by_name.get(job["name"], [])
        if len(matches) > 1:
            raise ReconcileError(
                f"managed cron name {job['name']} matches {len(matches)} live jobs; refusing to guess"
            )
        current = matches[0] if matches else None
        if job["state"] == "absent":
            if current:
                actions.append({"action": "remove", "id": current.get("id"), "job": job})
            continue
        if current is None:
            actions.append({"action": "add", "job": job})
            continue

        config_drift = _owned_current(current) != _owned_desired(job)
        enabled_drift = current.get("enabled") is not job["enabled"]
        if config_drift or enabled_drift:
            actions.append(
                {
                    "action": "edit",
                    "id": current.get("id"),
                    "job": job,
                    "configDrift": config_drift,
                }
            )

    for current in current_jobs:
        if (
            current.get("agentId") in paused_agents
            and current.get("name") not in desired_names
            and current.get("enabled") is True
        ):
            actions.append(
                {
                    "action": "disable-unmanaged",
                    "id": current.get("id"),
                    "job": {"name": current.get("name"), "agentId": current.get("agentId")},
                }
            )
    return actions


def _parse_cli_json(output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("jobs"), list):
            return value
    raise ReconcileError("openclaw cron list did not return a JSON jobs object")


def _redact(text: str, values: list[str]) -> str:
    redacted = text
    for value in values:
        if value:
            redacted = redacted.replace(value, "<REDACTED>")
    return redacted


def _run(args: list[str], *, retry: bool, sensitive: list[str]) -> str:
    transient = ("refused", "econnrefused", "not ready", "unavailable", "closed", "timed out", "timeout", "connect")
    attempts = 5 if retry else 1
    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run(args, text=True, capture_output=True, timeout=60, check=False)
        except subprocess.TimeoutExpired as error:
            output = str(error)
            result_code = 124
        else:
            output = "\n".join(part for part in (result.stdout, result.stderr) if part)
            result_code = result.returncode
            if result_code == 0:
                if result.stderr.strip():
                    print(_redact(result.stderr.strip(), sensitive), file=sys.stderr)
                return result.stdout
        lowered = output.lower()
        if attempt == attempts or not any(marker in lowered for marker in transient):
            safe = _redact(output.strip(), sensitive)
            raise ReconcileError(
                f"OpenClaw CLI failed on attempt {attempt}/{attempts} "
                f"with exit {result_code}: {safe[:1000]}"
            )
        time.sleep(attempt * 3)
    raise AssertionError("unreachable")


def _list_jobs(openclaw: str, sensitive: list[str]) -> list[dict[str, Any]]:
    output = _run(
        [openclaw, "cron", "list", "--all", "--json", "--timeout", "30000"],
        retry=True,
        sensitive=sensitive,
    )
    return _parse_cli_json(output)["jobs"]


def _job_edit_args(openclaw: str, action: dict[str, Any]) -> list[str]:
    job = action["job"]
    args = [openclaw, "cron", "edit", str(action["id"])]
    if action.get("configDrift"):
        args.extend(
            [
                "--name",
                job["name"],
                "--cron",
                job["schedule"],
                "--tz",
                job["timezone"],
                "--session",
                "isolated",
                "--agent",
                job["agentId"],
                "--message",
                job["message"],
                "--announce",
                "--channel",
                job["deliveryChannel"],
                "--to",
                job["deliveryTo"],
                "--wake",
                "now",
            ]
        )
    args.append("--enable" if job["enabled"] else "--disable")
    return args


def apply_plan(openclaw: str, actions: list[dict[str, Any]], sensitive: list[str]) -> None:
    for action in actions:
        kind = action["action"]
        job = action["job"]
        if kind == "add":
            args = [
                openclaw,
                "cron",
                "add",
                "--name",
                job["name"],
                "--cron",
                job["schedule"],
                "--tz",
                job["timezone"],
                "--session",
                "isolated",
                "--agent",
                job["agentId"],
                "--message",
                job["message"],
                "--announce",
                "--channel",
                job["deliveryChannel"],
                "--to",
                job["deliveryTo"],
                "--wake",
                "now",
            ]
            if not job["enabled"]:
                args.append("--disabled")
        elif kind == "edit":
            args = _job_edit_args(openclaw, action)
        elif kind == "disable-unmanaged":
            args = [openclaw, "cron", "edit", str(action["id"]), "--disable"]
        elif kind == "remove":
            args = [openclaw, "cron", "remove", str(action["id"])]
        else:
            raise ReconcileError(f"unknown reconciliation action: {kind}")
        try:
            _run(args, retry=True, sensitive=sensitive)
        except ReconcileError as error:
            raise ReconcileError(f"{_action_summary(action)} failed: {error}") from error


def _action_summary(action: dict[str, Any]) -> str:
    return f"{action['action']}: {action['job']['name']}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--openclaw", default="openclaw")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--error-file")
    args = parser.parse_args()

    try:
        manifest = json.loads(Path(args.manifest).read_text())
        if not isinstance(manifest, dict):
            raise ReconcileError("manifest root must be an object")
        # Validate before walking potentially sensitive job fields. This turns
        # wrong-shape-but-valid JSON into a safe, actionable ReconcileError.
        prepare_manifest(manifest)
        sensitive = [os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")]
        for job in manifest.get("jobs", []):
            sensitive.extend([str(job.get("message", "")), str(job.get("deliveryTo", ""))])
        current = _list_jobs(args.openclaw, sensitive)
        actions = build_plan(manifest, current)
        if not actions:
            print("OK: cron jobs already match scheduled automation policy")
            return 0
        if args.check:
            for action in actions:
                print(_action_summary(action))
            print(f"NEEDS_UPDATE: {len(actions)} cron action(s)")
            return 2

        apply_plan(args.openclaw, actions, sensitive)
        remaining: list[dict[str, Any]] = []
        for attempt in range(1, 6):
            remaining = build_plan(manifest, _list_jobs(args.openclaw, sensitive))
            if not remaining:
                break
            time.sleep(attempt)
        if remaining:
            names = ", ".join(_action_summary(action) for action in remaining)
            raise ReconcileError(f"cron verification did not converge: {names}")
        for action in actions:
            print(_action_summary(action))
        print(f"UPDATED: {len(actions)} cron action(s); verification converged")
        return 0
    except (OSError, json.JSONDecodeError, ReconcileError) as error:
        message = f"ERROR: {error}"
        print(message, file=sys.stderr)
        if args.error_file:
            try:
                Path(args.error_file).write_text(message + "\n")
            except OSError as write_error:
                print(
                    f"ERROR: could not write redacted error file: {write_error}",
                    file=sys.stderr,
                )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
