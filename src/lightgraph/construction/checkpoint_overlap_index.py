"""Store a derived overlap index with independently verifiable, lazily read evidence."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing, contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from lightgraph.contracts import canonical
from lightgraph.errors import LightGraphError
from lightgraph.runtime.retention import atomic_text

from .artifact_io import artifact_step
from .paths import io_path

FILENAME = 'overlap-index-v1.sqlite3'
RECEIPT = 'overlap-index-v1.json'
_SECTIONS = {'families': ('evidence', 'aliases'), 'relations': ('evidence',)}


def _binding(record):
    return {name: record[name] for name in ('artifact_id', 'graph_digest', 'node_id', 'access_fingerprint')}


def _store_block(db, value):
    data = canonical(value).encode('utf-8')
    address = sha256(data).hexdigest()
    db.execute('INSERT OR IGNORE INTO blocks VALUES (?, ?)', (address, data))
    return address


@contextmanager
def _staging_database(path):
    try:
        with closing(sqlite3.connect(path)) as db:
            yield db
    except sqlite3.Error:
        raise LightGraphError('workflow_checkpoint_overlap_write_failed') from None


def install(directory, record, contributor):
    """Stage the database on the same volume and expose its directory and blocks together."""
    from .overlap_patterns import build_overlap_index

    index = build_overlap_index(contributor)
    with TemporaryDirectory(prefix='.overlap-', dir=io_path(directory), ignore_cleanup_errors=True) as tmp:
        path = Path(tmp) / FILENAME
        with _staging_database(path) as db:
            db.execute('CREATE TABLE blocks (address TEXT PRIMARY KEY, payload BLOB NOT NULL) WITHOUT ROWID')
            db.execute('CREATE TABLE manifest (singleton INTEGER PRIMARY KEY CHECK (singleton=1), address TEXT NOT NULL)')
            header = dict(index)
            for section, fields in _SECTIONS.items():
                header[section] = []
                for row in index[section]:
                    metadata = {k: v for k, v in row.items() if k not in fields}
                    metadata['blocks'] = {k: _store_block(db, row[k]) for k in fields if k in row}
                    header[section].append(metadata)
            address = _store_block(db, {'storage_version': 1, 'checkpoint': _binding(record), 'index': header})
            db.execute('INSERT INTO manifest VALUES (1, ?)', (address,))
            db.commit()
        if record.get('overlap_index_digest', address) != address:
            raise LightGraphError('workflow_checkpoint_invalid')
        artifact_step('checkpoint_overlap_install', path.replace, io_path(directory / FILENAME))
    return address


def _verified_bytes(db, address):
    record = db.execute('SELECT payload FROM blocks WHERE address=?', (address,)).fetchone()
    if record is None or not isinstance(record[0], bytes) or sha256(record[0]).hexdigest() != address:
        raise ValueError
    return record[0]


def _read_block(db, address):
    return json.loads(_verified_bytes(db, address))


def _connect(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)


class _IndexRow(Mapping):
    """Column and role metadata are resident. Touching an evidence field opens only its block."""

    def __init__(self, path, row):
        self._path = path
        self._fields = dict(row)
        self._blocks = self._fields.pop('blocks')
        self._cache = {}

    def __iter__(self):
        yield from self._fields
        yield from self._blocks

    def __contains__(self, key):
        return key in self._fields or key in self._blocks

    def __len__(self):
        return len(self._fields) + len(self._blocks)

    def __getitem__(self, key):
        if key in self._fields:
            return self._fields[key]
        if key in self._cache:
            return self._cache[key]
        address = self._blocks[key]
        try:
            with closing(_connect(self._path)) as db:
                value = _read_block(db, address)
        except (sqlite3.Error, ValueError, OSError, TypeError):
            raise LightGraphError('workflow_checkpoint_invalid') from None
        self._cache[key] = value
        return value


def commit_legacy_receipt(directory, record, address):
    atomic_text(directory / RECEIPT, canonical({'version': 1, 'checkpoint': _binding(record), 'digest': address}))


def read(directory, record, *, verify_blocks=False):
    path = directory / FILENAME
    expected = record.get('overlap_index_digest')
    try:
        if expected is None:
            receipt_path = directory / RECEIPT
            if not receipt_path.exists():
                # An interrupted legacy build may have installed the database but not its receipt.
                return None
            receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
            if receipt['version'] != 1 or receipt['checkpoint'] != _binding(record):
                raise ValueError
            expected = receipt['digest']
        with closing(_connect(path)) as db:
            address, = db.execute('SELECT address FROM manifest WHERE singleton=1').fetchone()
            if expected != address:
                raise ValueError
            header = _read_block(db, address)
        if header['storage_version'] != 1 or header['checkpoint'] != _binding(record):
            raise ValueError
        index = header['index']
        if (index['format_version'] != 1 or index['contributor_id'] != record['node_id']
                or index.get('access_fingerprint') != record['access_fingerprint']):
            raise ValueError
        if verify_blocks:
            # Explicit preparation checks reusable blocks without decoding the original graph.
            addresses = {address for section in _SECTIONS for row in index[section]
                         for address in row['blocks'].values()}
            with closing(_connect(path)) as db:
                for address in addresses:
                    _verified_bytes(db, address)
        for section in _SECTIONS:
            index[section] = [_IndexRow(path, row) for row in index[section]]
        return index
    except (sqlite3.Error, OSError, TypeError, ValueError, KeyError):
        raise LightGraphError('workflow_checkpoint_invalid') from None
