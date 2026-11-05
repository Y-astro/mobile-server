"""Copied JAR-only rollback proof with owned native process models, not an Android JVM."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

import scripts.release_activation as ra
import tests.test_release_activation as tra


class NativeJarRecoveryTest(unittest.TestCase):
    def test_copied_rollback_restores_jar_after_bundle_deletion(self):
        with tempfile.TemporaryDirectory(prefix='jar-recovery-') as temporary:
            root = Path(temporary)
            builder = tra.TestReleaseActivation()
            builder.test_root = root
            bundle, preconditions, namespace, transaction = builder._build_jar_fixture()
            home, prefix = namespace / 'home', namespace / 'usr'
            java = prefix / 'bin/java'
            java.parent.mkdir(parents=True, exist_ok=True)
            java.symlink_to(sys.executable)
            jar = home / 'suwayomi/bin/Suwayomi-Server.jar'
            original_bytes = jar.read_bytes()
            candidate_bytes = (bundle / ra.DEFAULT_SUWAYOMI_JAR_LOCAL).read_bytes()
            events = home / '.config/server_recovery/jar-events.jsonl'
            child_code = (
                'import hashlib,json,os,pathlib,sys,time; '
                f'p=pathlib.Path({str(events)!r}); p.parent.mkdir(parents=True,exist_ok=True); '
                "p.open('a').write(json.dumps({'pid':os.getpid(),'sha256':hashlib.sha256(pathlib.Path(sys.argv[-1]).read_bytes()).hexdigest()})+'\\n'); "
                'time.sleep(60)'
            )
            supervisor = Path(__file__).resolve().parents[1] / 'scripts/server_supervisor.py'
            # Exact owned argv matching stays native. Only launch/health endpoints
            # are replaced so Linux can model Java without Android dependencies.
            override = f'''
_proof_children = []
def launch(sid, cfg):
    if sid != 'suwayomi':
        raise RuntimeError('JAR profile attempted an unrelated launch: ' + sid)
    command = [str(PREFIX / 'bin/java'), '-c', {child_code!r},
               '-Dsuwayomi.tachidesk.config.server.rootDir=' + str(HOME / 'suwayomi/data'),
               '-jar', str(HOME / 'suwayomi/bin/Suwayomi-Server.jar')]
    child = subprocess.Popen(command, cwd=str(HOME / 'suwayomi'), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
    _proof_children.append(child)
    return child

def healthy(sid, cfg):
    if sid != 'suwayomi':
        return True
    for pid, identity in matching_processes(sid):
        try:
            arguments = (PROC / str(pid) / 'cmdline').read_bytes().decode().rstrip('\\0').split('\\0')
            if arguments[0] == str(PREFIX / 'bin/java') and arguments[-2:] == ['-jar', str(HOME / 'suwayomi/bin/Suwayomi-Server.jar')]:
                return True
        except (OSError, IndexError):
            pass
    return False
'''
            source = supervisor.read_text() + override
            live_supervisor = home / 'bin/server_supervisor.py'
            live_supervisor.write_text(source)
            live_supervisor.chmod(0o700)
            (bundle / 'scripts/server_supervisor.py').write_text(source)
            entry = next(e for e in preconditions['files'] if e['path'] == ra.SUPERVISOR_REMOTE)
            entry.update(sha256=hashlib.sha256(source.encode()).hexdigest(), bytes=len(source.encode()), mode='0o700')
            disabled = home / '.config/services_disabled/tor'
            disabled.parent.mkdir(parents=True, exist_ok=True)
            disabled.write_bytes(b'keep-disabled\n')
            disabled.chmod(0o600)
            disabled_stat = disabled.stat()
            children = []
            driver = ra.ta.TermuxLifecycleDriver(namespace_root=namespace)

            def wait_events(count):
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    rows = [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
                    if len(rows) >= count:
                        return rows
                    time.sleep(0.02)
                self.fail('Process model did not record its loaded JAR')

            try:
                prior = driver.launch('suwayomi', driver.services()['suwayomi'])
                children.append(prior)
                first = wait_events(1)[0]
                result = ra.apply(bundle, preconditions, transaction, namespace,
                                  driver=driver, health_timeout=5, profile='suwayomi-jar')
                self.assertEqual(result['state'], ra.STATE_COMPLETED)
                children.extend(driver._sup._proof_children)
                prior.wait(timeout=3)
                second = wait_events(2)[1]
                self.assertNotEqual(first['pid'], second['pid'])
                self.assertEqual(second['sha256'], hashlib.sha256(candidate_bytes).hexdigest())
                self.assertEqual(jar.read_bytes(), candidate_bytes)
                self.assertEqual(driver.matching_processes('suwayomi_scheduler'), [])
                shutil.rmtree(bundle)
                environment = {key: value for key, value in os.environ.items() if key not in ('PYTHONPATH', 'PYTHONHOME')}
                environment.update(HOME=str(home), PREFIX=str(prefix))
                command = [sys.executable, str(transaction / 'recovery_lifecycle/release_activation.py'),
                           'rollback', '--transaction-dir', str(transaction), '--namespace-root', str(namespace)]
                run = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True, timeout=15)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(json.loads(run.stdout)['state'], ra.STATE_ROLLED_BACK)
                third = wait_events(3)[2]
                self.assertEqual(third['sha256'], hashlib.sha256(original_bytes).hexdigest())
                self.assertNotEqual(third['pid'], second['pid'])
                self.assertEqual(jar.read_bytes(), original_bytes)
                self.assertEqual(stat.S_IMODE(jar.stat().st_mode), 0o644)
                records = driver.matching_processes('suwayomi')
                self.assertEqual([record[0] for record in records], [third['pid']])
                self.assertEqual(disabled.read_bytes(), b'keep-disabled\n')
                self.assertEqual(disabled.stat().st_ino, disabled_stat.st_ino)
                self.assertEqual(disabled.stat().st_mtime_ns, disabled_stat.st_mtime_ns)
                self.assertFalse((home / '.config/services_disabled/suwayomi').exists())
                command[2] = 'recover'
                repeated = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True, timeout=15)
                self.assertEqual(repeated.returncode, 0, repeated.stderr)
                self.assertEqual(json.loads(repeated.stdout)['status'], 'idempotent_success')
                self.assertEqual(len(wait_events(3)), 3)
            finally:
                driver.terminate(driver.matching_processes('suwayomi'))
                for child in children:
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        child.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
