"""Agreement acceptance primitive. Call only with a server-authenticated identity.

This module does not provide authentication, HTTP routes, payments or a UI.
"""
import hashlib
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent / 'agreements'
REQUIRED = {'store': 'store-v1', 'shopper': 'shopper-v1'}

class AgreementGate:
    def __init__(self, connection):
        self.db = connection
        self.db.execute('''CREATE TABLE IF NOT EXISTS agreement_events (
            id INTEGER PRIMARY KEY, actor TEXT NOT NULL, store TEXT NOT NULL,
            version TEXT NOT NULL, digest TEXT NOT NULL, action TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )''')

    def document(self, version):
        if version not in {*REQUIRED.values(), 'autopay-v1'}:
            raise ValueError('Unknown agreement version')
        text = (ROOT / (version + '.md')).read_text()
        return text, hashlib.sha256(text.encode()).hexdigest()

    def record(self, actor, role, version, digest, *, accepted,
               store='', authorized_representative=False):
        if not actor or role not in REQUIRED:
            raise ValueError('Authenticated actor and known role required')
        if role == 'store' and (not store or not authorized_representative):
            raise ValueError('Store identity and representative authority required')
        allowed = {REQUIRED[role]}
        if role == 'shopper':
            allowed.add('autopay-v1')
        if version not in allowed or digest != self.document(version)[1]:
            raise ValueError('Wrong role or stale document; display current terms')
        if accepted is not True:
            raise ValueError('Explicit affirmative acceptance required')
        with self.db:
            self.db.execute('''INSERT INTO agreement_events
                (actor, store, version, digest, action) VALUES (?,?,?,?,?)''',
                (actor, store, version, digest, 'accept'))

    def current(self, actor, role, *, store='', automatic=False):
        if not actor or role not in REQUIRED:
            return False
        if role == 'store' and not store:
            return False
        if automatic and role != 'shopper':
            return False
        versions = [REQUIRED[role]] + (['autopay-v1'] if automatic else [])
        for version in versions:
            row = self.db.execute('''SELECT digest, action FROM agreement_events
                WHERE actor=? AND store=? AND version=? ORDER BY id DESC LIMIT 1''',
                (actor, store, version)).fetchone()
            if not row or row != (self.document(version)[1], 'accept'):
                return False
        return True

    def disable_automatic_payment(self, actor, *, store=''):
        if not actor:
            raise ValueError('Authenticated actor required')
        with self.db:
            self.db.execute('''INSERT INTO agreement_events
                (actor, store, version, digest, action) VALUES (?,?,?,?,?)''',
                (actor, store, 'autopay-v1', self.document('autopay-v1')[1], 'withdraw'))
