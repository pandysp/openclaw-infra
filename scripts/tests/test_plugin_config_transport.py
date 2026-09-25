#!/usr/bin/env python3
"""Run the real credential tasks through the installed Ansible runtime."""
import copy
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "ansible/roles/plugins/tasks/main.yml"
BUILD = "Build and write plugin config JSON"
CONFIGURE = "Configure mcp-adapter plugin"
SELECTED = {"Write server tokens to temp files (de-duplicated by token_var)",
            "Write Claude setup token to temp file", BUILD, CONFIGURE}


def walk_tasks(tasks):
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from walk_tasks(task.get(section, []))


def replace_paths(value, root):
    if isinstance(value, str):
        return value.replace("/home/ubuntu", str(root / "home")).replace(
            "/tmp/ansible-", str(root / "secrets/ansible-"))
    if isinstance(value, list):
        return [replace_paths(item, root) for item in value]
    if isinstance(value, dict):
        return {key: replace_paths(item, root) for key, item in value.items()}
    return value


class PluginConfigTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ansible = shutil.which("ansible-playbook")
        if not cls.ansible:
            raise RuntimeError("Install the project's Ansible development dependency first")
        # Use the parser shipped with Ansible, not a separately installed YAML or
        # Jinja package. Ansible itself renders and executes the selected tasks.
        python = shlex.split(Path(cls.ansible).read_text().splitlines()[0].removeprefix("#!"))
        code = "import json,sys,yaml; print(json.dumps([yaml.safe_load(open(p)) for p in sys.argv[1:]]))"
        result = subprocess.run([*python, "-c", code, str(ROLE), str(ROOT / "ansible/group_vars/all.yml")],
                                capture_output=True, text=True, check=True)
        tasks, cls.defaults = json.loads(result.stdout)
        cls.all_tasks = tasks
        cls.tasks = [task for task in tasks if any(t.get("name") in SELECTED for t in walk_tasks([task]))]
        # The shebang may carry interpreter flags; ask that interpreter for its own path.
        cls.python = subprocess.run([*python, "-c", "import sys; print(sys.executable)"],
                                    capture_output=True, text=True, check=True).stdout.strip()
        cls.real_jq = shutil.which("jq")
        if not cls.real_jq:
            raise RuntimeError("jq is required")

    def run_tasks(self, case="fresh"):
        with tempfile.TemporaryDirectory(prefix="plugin-config-test-") as tmp:
            root = Path(tmp)
            home, secrets, bin_dir = root / "home", root / "secrets", root / "bin"
            for directory in (home, secrets, bin_dir):
                directory.mkdir(mode=0o700)
            state = home / ".openclaw"
            state.mkdir(mode=0o700)
            cfg = state / "openclaw.json"
            old = {"gateway": {"mode": "local", "port": 18789, "auth": {"token": "fixture-gateway"}},
                   "tools": {"exec": {"node": "node-test"}}, "session": {"scope": "per-sender"},
                   "plugins": {"entries": {"openclaw-mcp-adapter": {"enabled": True, "config": {
                       "toolPrefix": False, "servers": [{"name": "old-server", "transport": "stdio", "command": "false",
                                                            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "fixture-saved-pat"}}]}}}}}
            old["plugins"]["installs"] = {"openclaw-mcp-adapter": {
                "version": "0.1.6" if case == "upgrade" else "0.1.7",
                "installPath": str(home / "managed-adapter"), "source": "npm"}}
            if case in ("disabled", "enabled_only"):
                old["plugins"]["entries"]["openclaw-mcp-adapter"]["enabled"] = False
            if case == "enabled_only":
                old["plugins"]["entries"]["openclaw-mcp-adapter"]["config"] = {
                    "toolPrefix": True, "servers": [
                        {"name": name, "transport": "stdio", "command": "/usr/bin/false",
                         "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": token}}
                        for name, token in (("github", "fixture-main"), ("github-test", "fixture-test"))]}
            if case in ("global_disabled", "global_enabled"):
                old["plugins"]["enabled"] = case == "global_enabled"
            if case == "node_exec_empty_token":
                old["gateway"]["auth"]["token"] = ""
            with os.fdopen(os.open(cfg, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as file:
                json.dump(old, file)
            if case == "invalid_source":
                cfg.write_text("fixture-invalid-json")
            before = cfg.read_bytes()
            desired = secrets / "ansible-plugin-config"
            if case == "existing_0644":
                desired.write_text("old public file")
                desired.chmod(0o644)
            target = root / "symlink-target"
            if case == "symlink":
                target.write_text("untouched")
                desired.symlink_to(target)
            jq = bin_dir / "jq"
            jq.write_text("#!" + self.python + "\n" + '''import json,os,pathlib,stat,sys
root=pathlib.Path(os.environ['TEST_ROOT'])
if any('fixture-' in arg for arg in sys.argv):
 (root/'argv-leak').touch();raise SystemExit(91)
if os.environ['TEST_CASE']=='partial_read_failure':
 print(json.dumps({'servers': [{'env': {'GITHUB_PERSONAL_ACCESS_TOKEN': 'fixture-partial-pat'}}]}));raise SystemExit(92)
if os.environ['TEST_CASE']=='build_failure':
 print('fixture-parser-diagnostic',file=sys.stderr);raise SystemExit(92)
if 'toolPrefix:' in sys.argv[-1] and 'servers: .' in sys.argv[-1]:
 s=os.fstat(1)
 assert stat.S_ISREG(s.st_mode) and stat.S_IMODE(s.st_mode)==0o600 and s.st_uid==os.getuid(), 'Output not private before population'
 (root/'private-before-write').touch()
os.execv(os.environ['REAL_JQ'],[os.environ['REAL_JQ']]+sys.argv[1:])
''')
            jq.chmod(0o700)
            node = bin_dir / "node"
            node.write_text("#!" + self.python + "\n" + '''import json,os,pathlib,sys
root=pathlib.Path(os.environ['TEST_ROOT'])
cfg=json.loads((root/'home/.openclaw/openclaw.json').read_text())
assert sys.argv[1] == cfg['plugins']['installs']['openclaw-mcp-adapter']['installPath']+'/dist/prepare.js', 'Did not use native installation metadata'
assert cfg['plugins']['enabled'] is False, 'Preparation requires disabled global plugin loading'
assert (root/'stopped').exists(), 'Preparation requires a stopped gateway'
desired=json.load(sys.stdin)
assert isinstance(desired,dict) and isinstance(desired['servers'],list)
if os.environ['TEST_CASE']=='prepare_failure':
 print('fixture-private-discovery-failure',file=sys.stderr);raise SystemExit(1)
changed=not (root/'prepared').exists()
(root/'prepared').touch()
print(json.dumps({'prepared':True,'changed':changed,'servers':len(desired['servers']),'tools':2}))
''')
            node.chmod(0o700)
            cli = bin_dir / "openclaw"
            cli.write_text("#!" + self.python + "\n" + '''import json,os,pathlib,sys
root=pathlib.Path(os.environ['TEST_ROOT']);args=sys.argv[1:]
if any('fixture-' in arg for arg in args):
 (root/'argv-leak').touch();raise SystemExit(91)
with (root/'cli-calls').open('a') as f:f.write(json.dumps(args)+'\\n')
p=root/'home/.openclaw/openclaw.json';cfg=json.loads(p.read_text())
if args==['plugins','list','--json']:
 install=cfg['plugins']['installs']['openclaw-mcp-adapter']
 print(json.dumps({'plugins':[{'id':'openclaw-mcp-adapter','version':install['version'],'rootDir':install['installPath']}]}));raise SystemExit(0)
elif args==['plugins','inspect','openclaw-mcp-adapter','--json']:
 print(json.dumps({'install':cfg['plugins']['installs']['openclaw-mcp-adapter']}));raise SystemExit(0)
elif args==['gateway','stop','--json']:
 (root/'stopped').touch();print('{}');raise SystemExit(0)
elif args==['gateway','start','--json']:
 assert cfg['plugins']['entries']['openclaw-mcp-adapter']['enabled'] is False
 assert 'enabled' not in cfg['plugins'], 'Recovery did not restore global loading'
 (root/'recovered').touch();print('{}');raise SystemExit(0)
elif args[:3]==['plugins','install','--force']:
 assert cfg['plugins']['enabled'] is False and (root/'stopped').exists()
 cfg['plugins']['entries']['openclaw-mcp-adapter']['enabled']=True
 cfg['plugins']['installs']['openclaw-mcp-adapter']['version']='0.1.7'
 (root/'installed').touch()
elif args==['config','set','plugins.entries.openclaw-mcp-adapter.enabled','false']:
 cfg['plugins']['entries']['openclaw-mcp-adapter']['enabled']=False
elif args[:3]==['config','set','plugins.enabled']:
 cfg['plugins']['enabled']=json.loads(args[3])
elif args==['config','unset','plugins.enabled']:
 del cfg['plugins']['enabled']
elif args==['config','patch','--stdin','--replace-path','plugins.entries.openclaw-mcp-adapter.config']:
 assert (root/'prepared').exists(), 'Enabled plugin before preparing its cache'
 patch=json.load(sys.stdin)
 with os.fdopen(os.open(root/'cli-stdin',os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600),'w') as f:json.dump(patch,f)
 entry=cfg['plugins']['entries']['openclaw-mcp-adapter']
 entry['config']=patch['plugins']['entries']['openclaw-mcp-adapter']['config']
 if 'enabled' in patch['plugins']['entries']['openclaw-mcp-adapter']:entry['enabled']=patch['plugins']['entries']['openclaw-mcp-adapter']['enabled']
elif args==['config','set','plugins.entries.openclaw-mcp-adapter.enabled','true']:
 cfg['plugins']['entries']['openclaw-mcp-adapter']['enabled']=True
else:raise AssertionError('Unexpected CLI arguments')
p.write_text(json.dumps(cfg))
''')
            cli.chmod(0o700)
            variables = copy.deepcopy(self.defaults)
            variables.update({
                "github_mcp_binary": {"stdout": "/usr/bin/false"},
                "github_token": "fixture-main\n\n", "github_token_test": "fixture-test\n",
                "claude_setup_token": "", "codex_proxy_gateway_ip": "", "qmd_workspaces": [],
                "node_exec_enabled": case.startswith("node_exec") or case == "minimal_node_exec",
                "node_exec_mcp_binary": {"stdout": "/usr/bin/true"},
                "_tailscale_wss_url": "wss://openclaw-test.example",
                "_openclaw_mcp_servers": [
                    {"type": "github", "name": "github", "token_var": "github_token"},
                    {"type": "github", "name": "github-test", "token_var": "github_token_test"},
                    {"type": "codex", "name": "codex", "cwd": "/workspace", "agent_id": "main"},
                    {"type": "claude-code", "name": "claude", "cwd": "/workspace", "agent_id": "main"},
                    {"type": "pi", "name": "pi", "cwd": "/workspace", "agent_id": "main"},
                    {"type": "node-exec", "name": "node-exec"},
                ],
                "ansible_python_interpreter": self.python,
            })
            if case in ("minimal_servers", "minimal_node_exec"):
                variables["_openclaw_mcp_servers"] = [server for server in variables["_openclaw_mcp_servers"] if server["type"] == "github"]
            if case == "force_reinstall":
                variables["force_plugin_reinstall"] = True
            tasks = replace_paths(copy.deepcopy(self.tasks), root)
            # Corrupt only the external desired-file boundary after a successful
            # build; the production comparison and cleanup remain unchanged.
            invalid_inputs = {"invalid_input": "invalid", "empty_input": "", "null_input": "null", "array_input": "[]"}
            if case in invalid_inputs:
                for task in walk_tasks(tasks):
                    if task.get("name") == BUILD:
                        task["ansible.builtin.shell"] += "\nprintf '%s' " + shlex.quote(invalid_inputs[case]) + " > " + shlex.quote(str(desired)) + "\n"
            play = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
                     "vars": variables, "tasks": tasks,
                     "handlers": [{"name": "restart openclaw-gateway", "ansible.builtin.debug": {"msg": "Restart boundary reached"}}]}]
            playbook = root / "play.json"
            with os.fdopen(os.open(playbook, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as file:
                json.dump(play, file)
            env = {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "HOME": str(home),
                   "TEST_ROOT": tmp, "TEST_CASE": case, "REAL_JQ": self.real_jq,
                   "ANSIBLE_LOCAL_TEMP": str(root / "ansible-control"),
                   "ANSIBLE_REMOTE_TEMP": str(root / "ansible-target"), "ANSIBLE_NOCOLOR": "1"}
            command = [self.ansible, "-i", "localhost,", str(playbook)]
            result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True,
                                    timeout=60, umask=0o022)
            self.assertFalse("fixture-" in result.stdout + result.stderr, "Synthetic credential appeared in task output")
            self.assertFalse((root / "argv-leak").exists(), "Credential reached process argv")
            success = case in ("fresh", "existing_0644", "node_exec", "minimal_servers", "minimal_node_exec",
                               "disabled", "enabled_only", "upgrade", "force_reinstall", "global_disabled", "global_enabled")
            if success:
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue((root / "private-before-write").exists())
                actual = json.loads(cfg.read_text())
                self.assertEqual(actual["session"], old["session"])
                self.assertTrue(actual["gateway"] == old["gateway"], "Unrelated gateway config changed")
                self.assertTrue(actual["plugins"]["entries"]["openclaw-mcp-adapter"]["enabled"])
                self.assertEqual(actual["plugins"].get("enabled"), old["plugins"].get("enabled"))
                self.assertEqual("enabled" in actual["plugins"], "enabled" in old["plugins"])
                self.assertEqual((root / "installed").exists(), case in ("upgrade", "force_reinstall"))
                config = actual["plugins"]["entries"]["openclaw-mcp-adapter"]["config"]
                self.assertEqual(config["toolPrefix"], True)
                names = ["github", "github-test"] + (["node-exec"] if case == "node_exec" else [])
                self.assertEqual([s["name"] for s in config["servers"]], names)
                if case == "node_exec":
                    self.assertTrue(config["servers"][2]["env"]["OPENCLAW_GATEWAY_TOKEN"] == "fixture-gateway")
                self.assertTrue(config["servers"][0]["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "fixture-main")
                self.assertTrue(config["servers"][1]["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "fixture-test")
                calls = [json.loads(line) for line in (root / "cli-calls").read_text().splitlines()]
                config_writes = [call for call in calls if call[-1] == "plugins.entries.openclaw-mcp-adapter.config"]
                self.assertEqual(len(config_writes), 1, "Config and enabled state should use one patch")
                rerun = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=60, umask=0o022)
                self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
                self.assertNotIn("fixture-", rerun.stdout + rerun.stderr)
                repeated = [json.loads(line) for line in (root / "cli-calls").read_text().splitlines()]
                self.assertEqual([call for call in repeated if call[-1] == "plugins.entries.openclaw-mcp-adapter.config"],
                                 config_writes, "Unchanged plugin config invoked writer again")
            elif case == "prepare_failure":
                self.assertNotEqual(result.returncode, 0)
                actual = json.loads(cfg.read_text())
                expected = copy.deepcopy(old)
                expected["plugins"]["entries"]["openclaw-mcp-adapter"]["enabled"] = False
                self.assertEqual(actual, expected, "Failure must disable only the adapter and restore global loading")
                self.assertNotIn("Restart boundary reached", result.stdout)
                self.assertTrue((root / "recovered").exists(), "Failed preparation left the gateway stopped")
                retry = subprocess.run(command, cwd=root, env={**env, "TEST_CASE": "fresh"},
                                       capture_output=True, text=True, timeout=60)
                self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
                retried = json.loads(cfg.read_text())
                self.assertNotIn("enabled", retried["plugins"], "Retry kept all plugins disabled")
                self.assertTrue(retried["plugins"]["entries"]["openclaw-mcp-adapter"]["enabled"])
            else:
                self.assertNotEqual(result.returncode, 0, "Invalid input was accepted")
                self.assertTrue(cfg.read_bytes() == before, "Invalid desired input changed config")
            self.assertFalse(desired.exists() or desired.is_symlink(), "Temporary plugin config survived")
            self.assertFalse(list(secrets.glob("ansible-mcp-token-*")), "Temporary PAT survived")
            if case == "symlink":
                self.assertEqual(target.read_text(), "untouched")

    def test_upgrade_and_force_reinstall_require_stopped_gateway_and_disabled_loading(self):
        for case in ("upgrade", "force_reinstall"):
            with self.subTest(case=case):
                self.run_tasks(case)

    def test_global_loading_policy_is_restored_after_preparation(self):
        for case in ("global_disabled", "global_enabled"):
            with self.subTest(case=case):
                self.run_tasks(case)

    def test_failed_preparation_restores_global_policy_and_can_retry(self):
        self.run_tasks("prepare_failure")

    def test_config_read_fails_closed(self):
        for case in ("invalid_source", "partial_read_failure"):
            with self.subTest(case=case):
                self.run_tasks(case)

    def test_config_and_enabled_state_use_one_idempotent_patch(self):
        for case in ("disabled", "enabled_only"):
            with self.subTest(case=case):
                self.run_tasks(case)

    def test_fresh_private_output_and_idempotent_apply(self):
        self.run_tasks()

    def test_existing_public_output_becomes_private_before_population(self):
        self.run_tasks("existing_0644")

    def test_optional_server_types_can_be_absent(self):
        for case in ("minimal_servers", "minimal_node_exec"):
            with self.subTest(case=case):
                self.run_tasks(case)

    def test_node_exec_gateway_token_does_not_reach_process_arguments(self):
        self.run_tasks("node_exec")

    def test_node_exec_requires_a_gateway_token(self):
        self.run_tasks("node_exec_empty_token")

    def test_symlink_is_rejected_without_touching_its_target(self):
        self.run_tasks("symlink")

    def test_build_failure_still_cleans_up_private_tokens(self):
        self.run_tasks("build_failure")

    def test_invalid_or_empty_desired_json_cannot_change_live_config(self):
        for case in ("invalid_input", "empty_input", "null_input", "array_input"):
            with self.subTest(case=case):
                self.run_tasks(case)


if __name__ == "__main__":
    unittest.main()
