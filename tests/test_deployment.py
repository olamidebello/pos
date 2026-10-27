import pathlib
import subprocess
import tempfile
import unittest
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from deploy.preflight import conflicting_ports, duplicate_hosts
from deploy.push_preflight import check

class PortAndRoutingTest(unittest.TestCase):
    def test_only_web_port_conflicts_are_reported(self):
        lines = '\n'.join([
            'LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=1,fd=1))',
            'LISTEN 0 511 *:443 *:* users:(("apache2",pid=2,fd=1))',
            'LISTEN 0 511 0.0.0.0:8080 *:* users:(("python",pid=3,fd=1))',
            'LISTEN 0 511 [::]:443 [::]:*'])
        conflicts = conflicting_ports(lines)
        self.assertEqual(len(conflicts), 2)
        self.assertIn('apache2', conflicts[0])
    def test_same_domain_in_other_vhost_is_rejected(self):
        output = '''# configuration file /etc/nginx/sites-available/pos-managed.conf:
server_name shop.test;
# configuration file /etc/nginx/conf.d/other.conf:
server_name unrelated.test shop.test;
'''
        self.assertEqual(duplicate_hosts(output, 'shop.test', '/etc/nginx/sites-available/pos-managed.conf'), ['/etc/nginx/conf.d/other.conf'])
        self.assertEqual(duplicate_hosts(output, 'other.test', '/etc/nginx/sites-available/pos-managed.conf'), [])

class NginxTemplateTest(unittest.TestCase):
    def render(self, tls, ipv6):
        templates = pathlib.Path(__file__).resolve().parents[1] / 'ansible' / 'templates'
        env = Environment(loader=FileSystemLoader(str(templates)), undefined=StrictUndefined)
        return env.get_template('nginx.conf.j2').render(
            pos_domain='shop.test', pos_tls_ready=tls,
            ansible_all_ipv6_addresses=['::1'] if ipv6 else [])
    def test_bootstrap_has_acme_and_no_premature_certificate_reference(self):
        text = self.render(False, False)
        self.assertIn('acme-challenge', text)
        self.assertNotIn('ssl_certificate ', text)
        self.assertNotIn('[::]', text)
        self.assertNotIn('listen 443', text)
    def test_tls_has_redirect_and_correct_domain_certificate(self):
        text = self.render(True, True)
        self.assertIn('return 301 https://shop.test$request_uri;', text)
        self.assertIn('/etc/letsencrypt/live/shop.test/fullchain.pem;', text)
        self.assertIn('listen [::]:443 ssl;', text)
        self.assertIn('root /var/www/pos/current;', text)

class PushSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.run_git('init', '-b', 'main')
        self.run_git('config', 'user.name', 'Test')
        self.run_git('config', 'user.email', 'test@example.invalid')
        self.run_git('remote', 'add', 'origin', 'https://github.com/olamidebello/pos.git')
        (self.root / 'README.md').write_text('Test\n')
        self.run_git('add', 'README.md')
        self.run_git('commit', '-m', 'Initial')
    def run_git(self, *args):
        subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True)
    def validate(self, paths, branch='main'):
        return check(self.root,branch,'origin','https://github.com/olamidebello/pos.git',paths)
    def test_accept_explicit_paths_and_reject_wrong_branch(self):
        self.assertEqual(self.validate(['README.md']), ['README.md'])
        with self.assertRaises(ValueError):
            self.validate(['README.md'], 'other')
    def test_reject_unrelated_staged_file(self):
        (self.root / 'other.txt').write_text('unrelated')
        self.run_git('add','other.txt')
        with self.assertRaises(ValueError):
            self.validate(['README.md'])
    def test_reject_secret_file_path_and_directory_escape(self):
        (self.root / 'private.key').write_text('secret')
        for paths in [['private.key'],['..'],['.'],['/etc']]:
            with self.assertRaises(ValueError):
                self.validate(paths)
    def test_reject_key_embedded_in_selected_text(self):
        (self.root / 'README.md').write_text('-----BEGIN ' + 'PRIVATE KEY-----\nsecret')
        with self.assertRaises(ValueError):
            self.validate(['README.md'])

if __name__ == '__main__':
    unittest.main()
