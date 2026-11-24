"""Shared repertoire catalogue with validated Current/Previous workbooks."""
from datetime import datetime, timedelta
from pathlib import Path
import gzip
import io
import json
import os
import posixpath
import shutil
import socket
import threading
import zipfile
import zlib
import xml.etree.ElementTree as ET

SOURCE = Path('/sdcard/FamilyShare/database repertoire 202610.xlsx')
STATIC = Path.home() / '.config/dashboard/static'
FIELDS = [
    ('Composition', 'Composition'), ('rAga', 'Raga'), ('tALa', 'Tala'),
    ('Composer', 'Composer'), ('Comp_type', 'Composition type'),
    ('Language', 'Language'), ('Subject', 'Subject'), ('Repertoire', 'Repertoire'),
    ('pATAntara', 'Patantara'), ('ConcertFit', 'Concert fit'),
    ('Teaching', 'Teaching'), ('Rep date', 'Repertoire date'),
    ('Remarks', 'Remarks'), ('Reference', 'Reference'),
]
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
RID = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
_lock = threading.Lock()
_cache = None
UPLOAD_LIMIT = 8 * 1024 * 1024
_upload_slot = threading.BoundedSemaphore(1)


class CatalogueError(Exception):
    pass


def _xml(archive, name):
    raw = archive.read(name)
    if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
        raise CatalogueError('Unsupported workbook XML.')
    return ET.fromstring(raw)


def read_catalogue(path=SOURCE):
    with zipfile.ZipFile(path) as archive:
        if sum(z.file_size for z in archive.infolist()) > 64 * 1024 * 1024:
            raise CatalogueError('Workbook is too large to load safely.')
        book = _xml(archive, 'xl/workbook.xml')
        sheet = next((s for s in book.findall('s:sheets/s:sheet', NS) if s.get('name') == 'Data'), None)
        if sheet is None:
            raise CatalogueError('The workbook needs a sheet named Data.')
        rels = _xml(archive, 'xl/_rels/workbook.xml.rels')
        target = next((r.get('Target') for r in rels if r.get('Id') == sheet.get(RID) and r.get('TargetMode') != 'External'), None)
        if not target:
            raise CatalogueError('The Data sheet cannot be located.')
        sheet_path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
        if not sheet_path.startswith('xl/'):
            raise CatalogueError('Invalid worksheet location.')
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [''.join(t.text or '' for t in si.findall('.//s:t', NS)) for si in _xml(archive, 'xl/sharedStrings.xml')]
        properties = book.find('s:workbookPr', NS)
        epoch = datetime(1904, 1, 1) if properties is not None and properties.get('date1904') in ('1', 'true') else datetime(1899, 12, 30)
        headers, records, ids = None, [], set()
        for row in _xml(archive, sheet_path).findall('s:sheetData/s:row', NS):
            cells = {}
            for cell in row.findall('s:c', NS):
                column = ''.join(c for c in cell.get('r', '') if c.isalpha())
                value = cell.find('s:v', NS)
                text = value.text if value is not None and value.text is not None else ''
                if cell.get('t') == 's':
                    text = strings[int(text)]
                elif cell.get('t') == 'inlineStr':
                    text = ''.join(t.text or '' for t in cell.findall('.//s:t', NS))
                elif cell.get('t') == 'e':
                    raise CatalogueError('The Data sheet contains an Excel error.')
                cells[column] = text.strip()
            if headers is None:
                if not any(cells.values()):
                    continue
                headers = {value: key for key, value in cells.items()}
                if not {'No.', *[f[0] for f in FIELDS]}.issubset(headers):
                    raise CatalogueError('The Data sheet is missing required columns.')
                continue
            if not cells.get(headers['Composition']):
                continue
            record = {key: cells.get(headers[key], '') for key, _ in FIELDS}
            ident = cells.get(headers['No.'], '')
            if not ident or ident in ids:
                raise CatalogueError('Each composition needs a unique No. value.')
            ids.add(ident)
            record['id'] = ident
            date = record['Rep date']
            if date:
                try:
                    record['Rep date'] = (epoch + timedelta(days=float(date))).date().isoformat()
                except ValueError:
                    pass  # Preserve existing textual dates rather than guessing their format.
            records.append(record)
            if len(records) > 50000:
                raise CatalogueError('The catalogue exceeds 50,000 compositions.')
        if not records:
            raise CatalogueError('The Data sheet has no compositions.')
        return records


def _signature(path):
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


class VersionStore:
    def __init__(self, folder, settings, legacy):
        self.folder, self.settings, self.legacy = Path(folder), Path(settings), Path(legacy)
        self.staging = self.folder.with_name('.repertoire-upload')
        self.lock = threading.RLock()

    @staticmethod
    def _write(path, data):
        with path.open('wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _sync_directory(path):
        if os.name == 'posix':
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _active(self, version):
        temp = self.settings / 'active.tmp'
        self._write(temp, version.encode())
        os.replace(temp, self.settings / 'active')
        self._sync_directory(self.settings)

    def _recover(self):
        pending = self.settings / 'pending'
        if pending.exists():
            # Staged files survive interruption; consumed files are already published.
            for name in ('previous.xlsx', 'current.xlsx'):
                staged = self.staging / name
                if staged.exists():
                    os.replace(staged, self.folder / name)
            self._sync_directory(self.folder)
            self._sync_directory(self.staging)
            self._active('current')
            pending.unlink()
            self._sync_directory(self.settings)
        if self.staging.exists():
            shutil.rmtree(self.staging)
            self._sync_directory(self.staging.parent)

    def _prepare(self):
        for directory in (self.folder, self.settings):
            if not directory.exists():
                directory.mkdir(parents=True)
                self._sync_directory(directory.parent)
        self._recover()
        current = self.folder / 'current.xlsx'
        if not current.exists() and self.legacy.exists():
            # Migration preserves bytes; reading/selection still validates them.
            os.replace(self.legacy, current)
            self._sync_directory(self.folder)
            self._sync_directory(self.legacy.parent)

    def metadata(self):
        with self.lock:
            self._prepare()
            try:
                active = (self.settings / 'active').read_text().strip()
            except FileNotFoundError:
                active = 'current'
            if active not in ('current', 'previous'):
                raise CatalogueError('Choose Current or Previous to repair the workbook selection.')
            result = {'active': active, 'limit': UPLOAD_LIMIT}
            for version in ('current', 'previous'):
                path = self.folder / (version + '.xlsx')
                result[version] = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat() if path.exists() else None
            return result

    def select(self, version):
        if version not in ('current', 'previous'):
            raise CatalogueError('Choose Current or Previous.')
        with self.lock:
            self._prepare()
            path = self.folder / (version + '.xlsx')
            if not path.exists():
                raise CatalogueError('Previous becomes available after the first upload.')
            read_catalogue(path)
            self._active(version)
            return self.metadata()

    def upload(self, data):
        if not data or len(data) > UPLOAD_LIMIT:
            raise CatalogueError('Choose an Excel workbook up to 8 MB.')
        try:
            read_catalogue(io.BytesIO(data))
        except (OSError, ValueError, IndexError, KeyError, OverflowError, RuntimeError, zlib.error, zipfile.BadZipFile, ET.ParseError) as error:
            raise CatalogueError('Choose a valid Excel workbook with a Data sheet and the existing columns.') from error
        with self.lock:
            self._prepare()
            self.staging.mkdir()
            try:
                self._write(self.staging / 'current.xlsx', data)
                current = self.folder / 'current.xlsx'
                if current.exists():
                    self._write(self.staging / 'previous.xlsx', current.read_bytes())
                self._sync_directory(self.staging)
                self._sync_directory(self.staging.parent)
                self._write(self.settings / 'pending', b'publish')
                self._sync_directory(self.settings)
                self._recover()
            finally:
                if not (self.settings / 'pending').exists() and self.staging.exists():
                    shutil.rmtree(self.staging)
            return self.metadata()


STORE = VersionStore(SOURCE.parent / 'Repertoire', Path.home() / '.config/repertoire/versions', SOURCE)


def catalogue_bytes(path=None):
    if path is None:
        with STORE.lock:
            versions = STORE.metadata()
            return _catalogue_bytes(STORE.folder / (versions['active'] + '.xlsx'), versions)
    return _catalogue_bytes(Path(path), None)


def _catalogue_bytes(path, versions):
    global _cache
    path = Path(path)
    with _lock:
        signature = _signature(path)
        key = str(path), signature, json.dumps(versions, sort_keys=True)
        if _cache is None or _cache[0] != key:
            records = read_catalogue(path)
            if _signature(path) != signature:
                raise CatalogueError('The workbook is being updated. Please retry shortly.')
            payload = {'fields': [{'key': k, 'label': label} for k, label in FIELDS],
                       'records': records, 'source': path.name,
                       'revision': str(path) + ':' + ':'.join(map(str, signature)) + ':' + key[2],
                       'updated': datetime.fromtimestamp(signature[2] / 1e9).astimezone().isoformat()}
            if versions is not None:
                payload['versions'] = versions
            raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
            _cache = key, raw, gzip.compress(raw)
        return _cache[1:]


def dispatch_repertoire(handler, user):
    path = handler.path.split('?', 1)[0]
    routes = {'/repertoire': ('repertoire.html', 'text/html; charset=utf-8'),
              '/repertoire/': ('repertoire.html', 'text/html; charset=utf-8'),
              '/repertoire/app.js': ('repertoire.js', 'text/javascript; charset=utf-8'),
              '/repertoire/app.css': ('repertoire.css', 'text/css; charset=utf-8')}
    if path not in routes and path not in ('/api/repertoire', '/api/repertoire/versions'):
        return False
    status, extra = 200, {}
    if not user:
        if path in ('/repertoire', '/repertoire/'):
            status, body, content_type, extra = 303, b'', 'text/plain', {'Location': '/login'}
        else:
            status, body, content_type = 401, b'{"message":"Please sign in to use the repertoire finder."}', 'application/json'
    else:
        try:
            if path == '/api/repertoire/versions':
                body, content_type = json.dumps(STORE.metadata()).encode(), 'application/json; charset=utf-8'
            elif path == '/api/repertoire':
                raw, compressed = catalogue_bytes()
                use_gzip = 'gzip' in handler.headers.get('Accept-Encoding', '')
                body, content_type = compressed if use_gzip else raw, 'application/json; charset=utf-8'
                extra['Vary'] = 'Accept-Encoding'
                if use_gzip:
                    extra['Content-Encoding'] = 'gzip'
            else:
                name, content_type = routes[path]
                body = (STATIC / name).read_bytes()
        except (OSError, ValueError, IndexError, KeyError, OverflowError, RuntimeError, zlib.error, zipfile.BadZipFile, ET.ParseError, CatalogueError):
            status, content_type = 503, 'application/json; charset=utf-8'
            body = b'{"message":"The repertoire workbook is unavailable or invalid. Check the Data sheet in FamilyShare, then retry."}'
    handler.send_response(status)
    for key, value in {'Content-Type': content_type, 'Content-Length': str(len(body)),
                       'Cache-Control': 'no-store, private', 'X-Content-Type-Options': 'nosniff',
                       'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
                       **extra}.items():
        handler.send_header(key, value)
    handler.end_headers()
    if handler.command != 'HEAD':
        handler.wfile.write(body)
    return True


def dispatch_repertoire_post(handler, user):
    path = handler.path.split('?', 1)[0]
    if path not in ('/api/repertoire/upload', '/api/repertoire/select'):
        return False
    status, result, acquired = 200, {}, False
    try:
        if not user:
            status, result = 401, {'message': 'Please sign in to manage the repertoire.'}
        elif handler.headers.get('X-Repertoire-Request') != '1' or handler.headers.get('Sec-Fetch-Site') == 'cross-site':
            status, result = 403, {'message': 'Open the repertoire finder to change its workbook.'}
        else:
            length = handler.headers.get('Content-Length', '')
            if handler.headers.get('Transfer-Encoding') or not length.isdecimal() or int(length) <= 0:
                status, result = 400, {'message': 'Invalid upload length.'}
            elif int(length) > (UPLOAD_LIMIT if path.endswith('/upload') else 128):
                status, result = 413, {'message': 'Choose an Excel workbook up to 8 MB.'}
            elif not _upload_slot.acquire(blocking=False):
                status, result = 429, {'message': 'Another workbook change is in progress. Please retry.'}
            else:
                acquired = True
                connection = getattr(handler, 'connection', None)
                old_timeout = connection.gettimeout() if connection else None
                try:
                    if connection: connection.settimeout(20)
                    body = handler.rfile.read(int(length))
                finally:
                    if connection: connection.settimeout(old_timeout)
                if len(body) != int(length):
                    raise CatalogueError('The upload was incomplete. Please retry.')
                if path.endswith('/upload'):
                    versions = STORE.upload(body)
                    message = 'Uploaded. Current is now in use; the last workbook is saved as Previous.'
                else:
                    data = json.loads(body)
                    if not isinstance(data, dict): raise CatalogueError('Choose Current or Previous.')
                    versions = STORE.select(data.get('version'))
                    message = versions['active'].capitalize() + ' is now in use for everyone.'
                result = {'message': message, 'versions': versions}
    except (CatalogueError, ValueError, UnicodeError) as error:
        status, result = 400, {'message': str(error) if isinstance(error, CatalogueError) else 'Invalid workbook selection.'}
    except (TimeoutError, socket.timeout):
        status, result = 408, {'message': 'The upload stalled. Please retry.'}
    except (OSError, RuntimeError, zlib.error, zipfile.BadZipFile, ET.ParseError, IndexError, KeyError, OverflowError):
        status, result = 503, {'message': 'The workbook change could not finish. Refresh to recover, then retry if needed.'}
    finally:
        if acquired: _upload_slot.release()
    body = json.dumps(result).encode()
    handler.close_connection = True
    handler.send_response(status)
    for key, value in {'Content-Type': 'application/json; charset=utf-8', 'Content-Length': str(len(body)),
                       'Cache-Control': 'no-store, private', 'X-Content-Type-Options': 'nosniff', 'Connection': 'close'}.items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)
    return True
