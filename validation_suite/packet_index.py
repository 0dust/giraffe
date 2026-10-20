"""Disposable disk-backed indexes for offline packet evidence, never live traffic."""
from collections.abc import MutableMapping
import pickle
import shutil
import sqlite3


class PacketIndex(MutableMapping):
    """Values originate in this process; never open an existing pickle database."""

    def __init__(self, path):
        # Exclusive creation prevents loading a substituted/untrusted database.
        with path.open('xb'):
            pass
        self.connection = sqlite3.connect(path)
        self.connection.execute('PRAGMA cache_size=-4096')
        self.connection.execute('PRAGMA journal_mode=OFF')
        self.connection.execute('PRAGMA max_page_count=524288')
        self.connection.execute('CREATE TABLE entries (key TEXT PRIMARY KEY, value BLOB)')
        self.size = 0
        self.path, self.writes = path, 0

    def __getitem__(self, key):
        row = self.connection.execute('SELECT value FROM entries WHERE key=?', (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return pickle.loads(row[0])

    def __setitem__(self, key, value):
        self.writes += 1
        if self.writes % 10000 == 0 and shutil.disk_usage(self.path.parent).free < 10 * 1024**3:
            raise ValueError('Offline packet index would breach 10 GiB free-disk reserve')
        if key not in self:
            self.size += 1
        self.connection.execute('INSERT OR REPLACE INTO entries VALUES (?, ?)',
                                (key, pickle.dumps(value, protocol=5)))

    def __delitem__(self, key):
        if self.connection.execute('DELETE FROM entries WHERE key=?', (key,)).rowcount == 0:
            raise KeyError(key)
        self.size -= 1

    def __contains__(self, key):
        return self.connection.execute('SELECT 1 FROM entries WHERE key=?', (key,)).fetchone() is not None

    def __iter__(self):
        return (row[0] for row in self.connection.execute('SELECT key FROM entries'))

    def __len__(self):
        return self.size

    def append(self, key, value):
        group = self.get(key, [])
        group.append(value)
        self[key] = group

    def close(self):
        self.connection.commit()
        self.connection.close()
