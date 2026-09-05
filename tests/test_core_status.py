"""Core host reachability must not depend on application metrics."""
import subprocess
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import queue_router as qr


class CoreStatusTest(unittest.TestCase):
    def test_three_of_four_answer(self):
        down = qr.CONFIG['targets']['pippin']['ssh_host']
        with patch.object(qr, 'probe_core_host', side_effect=lambda address: address != down):
            status = qr.get_core_status()
        self.assertEqual((status['up'], status['total']), (3, 4))
        self.assertFalse(status['hosts']['pippin']['online'])

    def test_probe_exception_is_unknown_and_preserves_other_hosts(self):
        bad = qr.CONFIG['targets']['pippin']['ssh_host']
        def probe(address):
            if address == bad:
                raise PermissionError('ping not permitted')
            return True
        with patch.object(qr, 'probe_core_host', side_effect=probe):
            status = qr.get_core_status()
        self.assertIsNone(status['up'])
        self.assertEqual(status['total'], 4)
        self.assertIsNone(status['hosts']['pippin']['online'])
        self.assertTrue(status['hosts']['gandalf']['online'])

    def test_zero_requires_four_no_answers(self):
        with patch.object(qr, 'probe_core_host', return_value=False):
            self.assertEqual(qr.get_core_status()['up'], 0)

    def test_header_renders_partial_unknown_and_zero(self):
        source = Path(qr.__file__).read_text()
        script = source[source.index('// Core uses independent'):source.index('function tempClass')]
        for up, label, color in [(3, '3/4 up', '#ffbf00'), (None, '?/4 up', '#aaa'),
                                 (0, '0/4 up', 'var(--neon-red)'), (4, '4/4 up', 'var(--neon-green)')]:
            payload = json.dumps({'up': up, 'total': 4, 'hosts': {}})
            check = """
const el = {};
global.document = {getElementById: () => el};
""" + script + f"\nlastCoreStatus = {payload}; updateFleetSummary();" + "\nconsole.log(el.innerHTML);"
            with self.subTest(up=up):
                result = subprocess.run(['node', '-e', check], capture_output=True, text=True, check=True)
                self.assertIn(label, result.stdout)
                self.assertIn('color:' + color, result.stdout)

    def test_ping_exit_codes_and_execution_errors(self):
        for code, expected in [(0, True), (1, False)]:
            with self.subTest(code=code), patch.object(qr.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, '', '')):
                self.assertIs(qr.probe_core_host('example'), expected)
        with patch.object(qr.subprocess, 'run', return_value=subprocess.CompletedProcess([], 2, '', 'permission denied')):
            with self.assertRaisesRegex(RuntimeError, 'permission denied'):
                qr.probe_core_host('example')
        for error in [FileNotFoundError('ping'), subprocess.TimeoutExpired('ping', 5)]:
            with self.subTest(error=error), patch.object(qr.subprocess, 'run', side_effect=error):
                with self.assertRaises(type(error)):
                    qr.probe_core_host('example')


if __name__ == '__main__':
    unittest.main()
