#!/usr/bin/env python3
"""Execute the documented recipe; mock only the Pulumi/OpenClaw CLI boundary."""
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def local_cli_recipe(source=None):
    source = source if source is not None else (ROOT / "CLAUDE.md").read_text()
    section = source.split("## Local CLI\n", 1)[1].split("# Trigger pairing", 1)[0]
    return section[section.index("(\n  set -euo pipefail"):].replace("<tailnet>", "tail-test")


class LocalCliRecipeTests(unittest.TestCase):
    def run_recipe(self, output, status=0, existing=True, recipe=None):
        with tempfile.TemporaryDirectory(prefix="local-cli-recipe-") as tmp:
            root = Path(tmp)
            (root / "pulumi").mkdir()
            config = root / ".openclaw/openclaw.json"
            config.parent.mkdir(mode=0o700)
            before = {"gateway": {"mode": "remote", "remote": {"token": "fixture-old"}},
                      "session": {"scope": "per-sender"}}
            if existing:
                with os.fdopen(os.open(config, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as file:
                    json.dump(before, file)
            preimage = config.read_bytes() if existing else None
            bin_dir = root / "bin"
            bin_dir.mkdir()
            commands = {
                "pulumi": '''import os,sys
assert sys.argv[1:]==['stack','output','openclawGatewayToken','--stack','prod','--show-secrets']
sys.stdout.write(os.environ['PULUMI_OUTPUT'])
raise SystemExit(int(os.environ['PULUMI_STATUS']))
''',
                "openclaw": '''import json,os,pathlib,stat,sys
root=pathlib.Path(os.environ['HOME']);(root/'writer-called').touch()
assert not any('fixture-' in arg for arg in sys.argv), 'Secret reached argv'
assert sys.argv[1:]==['config','patch','--stdin']
patch=json.load(sys.stdin)
assert patch['gateway']['remote']['token'], 'Empty token reached writer'
config=root/'.openclaw/openclaw.json'
current=json.loads(config.read_text()) if config.exists() else {}
if config.exists():
 assert stat.S_IMODE(config.stat().st_mode)==0o600
 backup=config.with_suffix('.json.bak')
 with os.fdopen(os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w') as file:json.dump(current,file)
def merge(before,after):
 for key,value in after.items():
  if isinstance(value,dict) and isinstance(before.get(key),dict):merge(before[key],value)
  else:before[key]=value
merge(current,patch)
with os.fdopen(os.open(config,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600),'w') as file:json.dump(current,file)
''',
            }
            for name, body in commands.items():
                executable = bin_dir / name
                executable.write_text("#!/usr/bin/env python3\n" + body)
                executable.chmod(0o700)
            env = {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "HOME": tmp,
                   "PULUMI_OUTPUT": output, "PULUMI_STATUS": str(status)}
            result = subprocess.run(["bash", "-c", recipe or local_cli_recipe()], cwd=root,
                                    env=env, text=True, capture_output=True, timeout=15)
            self.assertNotIn("fixture-", result.stdout + result.stderr)
            after = config.read_bytes() if config.exists() else None
            called = (root / "writer-called").exists()
            for file in config.parent.glob("openclaw.json*"):
                self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
                self.assertEqual(file.stat().st_uid, os.getuid())
            return result, preimage, after, called

    def test_success_updates_existing_config_without_changing_other_settings(self):
        result, _, after, called = self.run_recipe("fixture-new\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(called)
        actual = json.loads(after)
        self.assertEqual(actual["gateway"]["remote"]["token"], "fixture-new")
        self.assertEqual(actual["gateway"]["remote"]["url"], "wss://openclaw-vps.tail-test.ts.net")
        self.assertEqual(actual["session"], {"scope": "per-sender"})

    def test_success_creates_a_private_config(self):
        result, before, after, called = self.run_recipe("fixture-new\n", existing=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(before)
        self.assertTrue(called)
        self.assertEqual(json.loads(after)["gateway"]["remote"]["token"], "fixture-new")

    def test_empty_or_failed_pulumi_cannot_change_existing_config(self):
        for output, status in (("", 0), ("", 1), ("fixture-partial\n", 1)):
            with self.subTest(output_nonempty=bool(output), status=status):
                result, before, after, called = self.run_recipe(output, status)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(after, before)
                if status:
                    self.assertFalse(called)

    def test_partial_output_regression_is_caught(self):
        # Reproduce the former direct pipeline: pipefail notices the failed
        # producer only after a downstream writer has already changed config.
        recipe = local_cli_recipe().replace(
            "  GATEWAY_TOKEN=$(cd pulumi && pulumi stack output openclawGatewayToken --stack prod --show-secrets)\n  printf '%s' \"$GATEWAY_TOKEN\" |",
            "  (cd pulumi && pulumi stack output openclawGatewayToken --stack prod --show-secrets) |",
        )
        self.assertNotEqual(recipe, local_cli_recipe())
        result, before, after, called = self.run_recipe("fixture-partial\n", 1, recipe=recipe)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(called)
        self.assertNotEqual(after, before)


if __name__ == "__main__":
    unittest.main()
