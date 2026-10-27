import grp
import os
import pathlib
import pwd
import stat
import subprocess
import sys
import tempfile
import unittest
import yaml
from deploy.check_public_access import check,worker_from_config,REQUIRED

ROOT=pathlib.Path(__file__).resolve().parents[1]
class PublicReleasePermissionsTest(unittest.TestCase):
    def test_remote_copy_is_followed_by_worker_readable_permissions(self):
        # Exercise the actual copy and normalization tasks against a dummy release,
        # including a pre-existing directory lacking traversal permission.
        play=yaml.safe_load((ROOT/'ansible/deploy.yml').read_text())[0]
        tasks=play['tasks'][0]['block']
        copy=next(t for t in tasks if t['name']=='Copy only public web assets into the release')
        normalize=next(t for t in tasks if t['name']=='Normalize traversal and read permissions only within the public release')
        with tempfile.TemporaryDirectory() as tmp:
            base=pathlib.Path(tmp);base.chmod(0o755)
            source=base/'source';source.mkdir()
            release=base/'release';release.mkdir();release.chmod(0o755)
            for name in REQUIRED:
                path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('public test asset')
            owner=pwd.getpwuid(os.geteuid()).pw_name;group=grp.getgrgid(os.getegid()).gr_name
            copy['ansible.builtin.copy'].update(src=str(source)+'/',dest=str(release)+'/',owner=owner,group=group)
            normalize['ansible.builtin.file'].update(path=str(release),owner=owner,group=group)
            path=base/'test.yml';path.write_text(yaml.safe_dump([dict(hosts='localhost',gather_facts=False,tasks=[copy,{'name':'Simulate legacy release without traversal permission','ansible.builtin.file':{'path':str(release),'mode':'0644'}},normalize])]))
            result=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i','localhost,','-c','local',str(path)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            for path in [release,*release.rglob('*')]:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o755 if path.is_dir() else 0o644)
            worker=owner
            self.assertEqual(check(release,worker)['readable_assets'],len(REQUIRED))
    def test_worker_parser_uses_explicit_configured_account(self):
        self.assertEqual(worker_from_config('# user root;\nuser www-data;\n'),'www-data')
        self.assertEqual(worker_from_config('user nginx nginx;'),'nginx')
        with self.assertRaises(ValueError):worker_from_config('# user nobody;')
