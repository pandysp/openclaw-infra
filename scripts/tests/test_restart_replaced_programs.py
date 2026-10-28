#!/usr/bin/env python3
"""Run restart-replaced-programs.sh against real processes whose program was deleted.

Linux only (/proc). systemctl and the cgroup tree are fixtures; the processes and
their /proc/<pid>/exe links are real.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT/'ansible/roles/system/files/restart-replaced-programs.sh'

# Serves the queries the script makes from units.json. `restart` swaps the unit's
# processes for a fresh one unless the unit is marked to stay stale.
SYSTEMCTL = '''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
state = Path(os.environ['FIXTURE'])
units = json.loads((state/'units.json').read_text())
args = sys.argv[1:]
assert args[0] == '--user', args
if args[1] == 'list-units':
    assert args[2:] == ['--type=service', '--state=running', '--no-legend', '--plain'], args
    for name in units:
        print(f'{name} loaded active running fixture')
elif args[1] == 'show':
    assert args[2] == '-P', args
    unit = units[args[4]]
    if args[3] in unit.get('broken', []):
        raise SystemExit('Failed to get properties: fixture failure')
    print({'FragmentPath': unit['fragment'], 'Type': unit['type'], 'ControlGroup': unit['cgroup']}[args[3]])
elif args[1] == 'restart':
    with (state/'restarts').open('a') as log:
        log.write(args[2] + '\\n')
    unit = units[args[2]]
    if not unit.get('stays_stale'):
        fresh = subprocess.Popen(['sleep', '300'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True).pid
        (state/'fresh').open('a').write(f'{fresh}\\n')
        procs = state/'cgroup'/unit['cgroup'].lstrip('/')
        for path in procs.rglob('cgroup.procs'):
            path.write_text('')
        (procs/'cgroup.procs').write_text(f'{fresh}\\n')
else:
    raise SystemExit(f'unexpected systemctl call: {args}')
'''


@unittest.skipUnless(sys.platform.startswith('linux'), 'Needs Linux /proc/<pid>/exe')
class RestartReplacedProgramsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='replaced-programs-'))
        self.processes = []
        self.units = 0
        self.addCleanup(self.cleanup)
        bin_dir = self.tmp/'bin'
        bin_dir.mkdir()
        (bin_dir/'systemctl').write_text(SYSTEMCTL)
        (bin_dir/'systemctl').chmod(0o755)
        self.env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'FIXTURE': str(self.tmp),
                    'CGROUP_ROOT': str(self.tmp/'cgroup'), 'UNIT_DIR': str(self.tmp/'units')}

    def cleanup(self):
        fresh = self.tmp/'fresh'
        pids = [p.pid for p in self.processes] + ([int(x) for x in fresh.read_text().split()] if fresh.exists() else [])
        for pid in pids:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        for process in self.processes:
            process.wait()
        shutil.rmtree(self.tmp)

    def process(self, replaced):
        """A real running process; `replaced` deletes its program afterwards, like apt does."""
        program = self.tmp/f'program-{len(self.processes)}'
        shutil.copy2(shutil.which('sleep'), program)
        process = subprocess.Popen([str(program), '300'])
        self.processes.append(process)
        # Wait until exec finished, so the link names the copied program.
        while os.readlink(f'/proc/{process.pid}/exe') != str(program):
            pass
        if replaced:
            program.unlink()
        return process.pid

    def run_script(self, units):
        for name, unit in units.items():
            procs = self.tmp/'cgroup'/unit['cgroup'].lstrip('/')
            for child, pids in unit.pop('procs').items():
                (procs/child).mkdir(parents=True, exist_ok=True)
                (procs/child/'cgroup.procs').write_text(''.join(f'{pid}\n' for pid in pids))
        (self.tmp/'units.json').write_text(json.dumps(units))
        result = subprocess.run(['bash', str(SCRIPT)], capture_output=True, text=True, env=self.env, timeout=60)
        restarts = self.tmp/'restarts'
        return result, restarts.read_text().split() if restarts.exists() else []

    def unit(self, procs, fragment_dir='units', kind='simple', **extra):
        self.units += 1
        return {'fragment': str(self.tmp/fragment_dir/f'unit-{self.units}.service'), 'type': kind,
                'cgroup': f'/user.slice/unit-{self.units}.service', 'procs': procs, **extra}

    def test_only_provisioned_long_running_services_on_replaced_programs_restart(self):
        units = {
            'gateway.service': self.unit({'.': [self.process(replaced=True)]}),
            'child-stale.service': self.unit({'.': [self.process(False)], 'worker': [self.process(True)]}),
            'current.service': self.unit({'.': [self.process(replaced=False)]}),
            'foreign.service': self.unit({'.': [self.process(True)]}, fragment_dir='elsewhere'),
            'job.service': self.unit({'.': [self.process(True)]}, kind='oneshot'),
        }
        result, restarts = self.run_script(units)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sorted(restarts), ['child-stale.service', 'gateway.service'])
        self.assertIn(f'RESTARTED gateway.service {self.tmp}/program-0\n', result.stdout)

    def test_nothing_replaced_restarts_nothing(self):
        result, restarts = self.run_script({'gateway.service': self.unit({'.': [self.process(False)]})})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((restarts, result.stdout), ([], ''))

    def test_a_service_still_stale_after_restart_fails_loudly(self):
        units = {'gateway.service': self.unit({'.': [self.process(True)]}, stays_stale=True)}
        result, restarts = self.run_script(units)
        self.assertEqual(restarts, ['gateway.service'])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('ERROR: still running a replaced program after restart', result.stderr)

    def test_a_service_that_cannot_be_inspected_fails_instead_of_counting_as_current(self):
        for broken in (['FragmentPath'], ['Type'], ['ControlGroup']):
            with self.subTest(broken=broken):
                (self.tmp/'restarts').unlink(missing_ok=True)
                units = {'current.service': self.unit({'.': [self.process(False)]}),
                         'gateway.service': self.unit({'.': [self.process(True)]}, broken=broken)}
                result, restarts = self.run_script(units)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(restarts, [])

    def test_a_running_service_without_its_control_group_fails(self):
        unit = self.unit({'.': [self.process(True)]})
        unit['cgroup'] = '/user.slice/missing.service'
        unit['procs'] = {}
        result, restarts = self.run_script({'gateway.service': unit})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('ERROR: cannot find the control group of running gateway.service', result.stderr)


if __name__ == '__main__':
    unittest.main()
