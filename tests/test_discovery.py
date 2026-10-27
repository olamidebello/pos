import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import discovery

class DiscoveryTests(unittest.TestCase):
    def test_coordinates_reject_injection_and_nonfinite(self):
        for lat in ('0);out;',float('nan'),91,float('inf')):
            with self.assertRaises(ValueError):discovery.coordinates({'lat':lat,'lon':0})
    def test_findings_never_invent_stock_or_prices(self):
        rows=discovery.parse_findings({'elements':[{'type':'node','id':1,'lat':6,'lon':3,'tags':{'name':'Water shop','shop':'convenience','addr:street':'High Street','price':'5','stock':'100'}}]})
        self.assertEqual(rows[0]['inventory'],[])
        self.assertFalse(rows[0]['verified'])
        self.assertEqual(rows[0]['source_url'],'https://www.openstreetmap.org/node/1')
    def test_ansible_runner_cache_and_private_cleanup(self):
        discovery.CACHE.clear()
        with tempfile.TemporaryDirectory() as folder:
            def run(args,**kwargs):
                self.assertEqual(args[0],'/usr/bin/ansible-playbook')
                data=json.loads(Path(args[-1][1:]).read_text())
                Path(data['discovery_output']).write_text(json.dumps({'findings':[]}))
                return type('Result',(),{'returncode':0})()
            with patch('discovery.subprocess.run',side_effect=run) as runner:
                self.assertEqual(discovery.discover({'lat':1,'lon':2},folder+'/db'),{'findings':[]})
                discovery.discover({'lat':1,'lon':2},folder+'/db')
                self.assertEqual(runner.call_count,1)
            self.assertEqual(list(Path(folder).iterdir()),[])
    def test_runner_failure_has_no_partial_results(self):
        discovery.CACHE.clear()
        with tempfile.TemporaryDirectory() as folder,patch('discovery.subprocess.run',return_value=type('Result',(),{'returncode':1})()):
            with self.assertRaisesRegex(ValueError,'unavailable'):discovery.discover({'lat':1,'lon':2},folder+'/db')
            self.assertEqual(list(Path(folder).iterdir()),[])
