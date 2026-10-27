import sqlite3
import unittest
from agreement_gate import AgreementGate

class AgreementsTest(unittest.TestCase):
    def setUp(self):
        self.g = AgreementGate(sqlite3.connect(':memory:'))
    def accept(self, actor, role, version, **kwargs):
        self.g.record(actor, role, version, self.g.document(version)[1], accepted=True, **kwargs)
    def test_shopper_requires_separate_autopay_consent_and_can_withdraw(self):
        self.assertFalse(self.g.current('alice', 'shopper'))
        self.accept('alice', 'shopper', 'shopper-v1')
        self.assertTrue(self.g.current('alice', 'shopper'))
        self.assertFalse(self.g.current('alice', 'shopper', automatic=True))
        self.accept('alice', 'shopper', 'autopay-v1')
        self.assertTrue(self.g.current('alice', 'shopper', automatic=True))
        self.g.disable_automatic_payment('alice')
        self.assertFalse(self.g.current('alice', 'shopper', automatic=True))
        self.assertTrue(self.g.current('alice', 'shopper'))
    def test_store_scope_and_authority(self):
        with self.assertRaises(ValueError):
            self.accept('owner', 'store', 'store-v1', store='a')
        self.accept('owner', 'store', 'store-v1', store='a', authorized_representative=True)
        self.assertTrue(self.g.current('owner', 'store', store='a'))
        self.assertFalse(self.g.current('owner', 'store', store='b'))
        self.assertFalse(self.g.current('other', 'store', store='a'))
    def test_reject_stale_wrong_role_and_nonaffirmative(self):
        with self.assertRaises(ValueError):
            self.g.record('a','shopper','shopper-v1','stale',accepted=True)
        with self.assertRaises(ValueError):
            self.accept('a','shopper','store-v1')
        with self.assertRaises(ValueError):
            self.g.record('a','shopper','shopper-v1',self.g.document('shopper-v1')[1],accepted=False)

if __name__ == '__main__':
    unittest.main()
