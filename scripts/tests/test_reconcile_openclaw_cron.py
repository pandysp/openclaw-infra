#!/usr/bin/env python3

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "ansible/roles/telegram/files/reconcile_openclaw_cron.py"
)
SPEC = importlib.util.spec_from_file_location("reconcile_openclaw_cron", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def desired_job(name="main: Daily", agent="main", enabled=None):
    job = {
        "name": name,
        "agentId": agent,
        "schedule": "0 9 * * *",
        "timezone": "Europe/Berlin",
        "message": "Daily check",
        "deliveryChannel": "telegram",
        "deliveryTo": "123",
    }
    if enabled is not None:
        job["enabled"] = enabled
    return job


def current_job(job, job_id="job-1", enabled=True):
    return {
        "id": job_id,
        "name": job["name"],
        "agentId": job["agentId"],
        "enabled": enabled,
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


class ReconcilePlanTests(unittest.TestCase):
    def manifest(self, agents, jobs):
        return {"scheduledAutomationDefault": False, "agents": agents, "jobs": jobs}

    def test_omitted_agent_switch_is_off_and_omitted_job_enabled_is_true(self):
        job = desired_job()
        plan = MODULE.build_plan(self.manifest([{"id": "main"}], [job]), [])
        self.assertEqual(plan[0]["action"], "add")
        self.assertFalse(plan[0]["job"]["enabled"])

    def test_enabled_agent_enables_declared_job(self):
        job = desired_job()
        agents = [{"id": "main", "scheduledAutomationEnabled": True}]
        plan = MODULE.build_plan(self.manifest(agents, [job]), [])
        self.assertTrue(plan[0]["job"]["enabled"])

    def test_job_level_false_survives_agent_resume(self):
        job = desired_job(enabled=False)
        agents = [{"id": "main", "scheduledAutomationEnabled": True}]
        plan = MODULE.build_plan(self.manifest(agents, [job]), [current_job(job, enabled=True)])
        self.assertEqual([(action["action"], action["id"]) for action in plan], [("edit", "job-1")])
        self.assertFalse(plan[0]["job"]["enabled"])

    def test_pause_disables_managed_and_unmanaged_jobs_without_removing_them(self):
        job = desired_job()
        unmanaged = current_job(desired_job("main: One-off"), job_id="manual-1", enabled=True)
        plan = MODULE.build_plan(
            self.manifest([{"id": "main", "scheduledAutomationEnabled": False}], [job]),
            [current_job(job, enabled=True), unmanaged],
        )
        self.assertEqual(
            [(action["action"], action["id"]) for action in plan],
            [("edit", "job-1"), ("disable-unmanaged", "manual-1")],
        )

    def test_resume_only_enables_declared_managed_job(self):
        job = desired_job()
        unmanaged = current_job(desired_job("main: One-off"), job_id="manual-1", enabled=False)
        plan = MODULE.build_plan(
            self.manifest([{"id": "main", "scheduledAutomationEnabled": True}], [job]),
            [current_job(job, enabled=False), unmanaged],
        )
        self.assertEqual([(action["action"], action["id"]) for action in plan], [("edit", "job-1")])
        self.assertTrue(plan[0]["job"]["enabled"])

    def test_field_drift_is_an_in_place_edit(self):
        job = desired_job()
        current = current_job(job, job_id="stable-id", enabled=True)
        current["schedule"]["expr"] = "5 9 * * *"
        plan = MODULE.build_plan(
            self.manifest([{"id": "main", "scheduledAutomationEnabled": True}], [job]),
            [current],
        )
        self.assertEqual(plan[0]["action"], "edit")
        self.assertEqual(plan[0]["id"], "stable-id")
        self.assertTrue(plan[0]["configDrift"])

    def test_converged_state_is_idempotent_and_unrelated_agents_are_untouched(self):
        job = desired_job()
        unrelated_job = desired_job("other: Daily", "other")
        manifest = self.manifest(
            [
                {"id": "main", "scheduledAutomationEnabled": True},
                {"id": "other", "scheduledAutomationEnabled": True},
            ],
            [job],
        )
        current = [current_job(job), current_job(unrelated_job, job_id="other-id")]
        self.assertEqual(MODULE.build_plan(manifest, current), [])

    def test_duplicate_live_managed_name_fails_closed(self):
        job = desired_job()
        with self.assertRaisesRegex(MODULE.ReconcileError, "refusing to guess"):
            MODULE.build_plan(
                self.manifest([{"id": "main"}], [job]),
                [current_job(job, "one"), current_job(job, "two")],
            )

    def test_explicit_absent_is_the_only_removal_path(self):
        tombstone = {"name": "main: Retired", "agentId": "main", "state": "absent"}
        live = current_job(desired_job("main: Retired"), job_id="retired-id")
        plan = MODULE.build_plan(self.manifest([{"id": "main"}], [tombstone]), [live])
        self.assertEqual([(action["action"], action["id"]) for action in plan], [("remove", "retired-id")])

    def test_invalid_boolean_and_unknown_agent_fail_before_planning(self):
        with self.assertRaisesRegex(MODULE.ReconcileError, "must be a boolean"):
            MODULE.build_plan(
                self.manifest([{"id": "main", "scheduledAutomationEnabled": "false"}], []),
                [],
            )
        with self.assertRaisesRegex(MODULE.ReconcileError, "unknown agent"):
            MODULE.build_plan(self.manifest([{"id": "main"}], [desired_job(agent="missing")]), [])

    def test_wrong_shape_manifest_and_live_jobs_fail_closed(self):
        with self.assertRaisesRegex(MODULE.ReconcileError, "manifest root"):
            MODULE.build_plan([], [])
        with self.assertRaisesRegex(MODULE.ReconcileError, "every job must be an object"):
            MODULE.build_plan(self.manifest([{"id": "main"}], ["not-an-object"]), [])

        job = desired_job()
        malformed = current_job(job)
        malformed["schedule"] = "not-an-object"
        with self.assertRaisesRegex(MODULE.ReconcileError, "non-object schedule"):
            MODULE.build_plan(self.manifest([{"id": "main"}], [job]), [malformed])

    def test_live_identity_and_enabled_types_are_validated(self):
        job = desired_job()
        malformed_id = current_job(job)
        malformed_id["id"] = None
        with self.assertRaisesRegex(MODULE.ReconcileError, "invalid id"):
            MODULE.build_plan(self.manifest([{"id": "main"}], [job]), [malformed_id])

        malformed_enabled = current_job(job)
        malformed_enabled["enabled"] = 1
        with self.assertRaisesRegex(MODULE.ReconcileError, "must be a boolean"):
            MODULE.build_plan(self.manifest([{"id": "main"}], [job]), [malformed_enabled])


if __name__ == "__main__":
    unittest.main()
