"""Owner-only, bounded, resumable backups into one deliberately mounted SMB folder."""
import hashlib, json, os, re, shutil, threading, time, unicodedata
from pathlib import Path
from urllib.parse import urlparse

BACKUP_CHUNK = 1024 * 1024
BACKUP_MAX = 8 * 1024 ** 3
BACKUP_SHARE = r'\\Mungoon\data\Backups\QRTransfer'

def backup_sync_directory(path):
    if os.name == 'posix':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)

class BackupError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message); self.status = status

class QRBackups:
    def __init__(self, data, root=None):
        self.root = Path(root or os.environ.get('QR_BACKUP_ROOT', '/backups'))
        self.meta = Path(data) / 'private/qr-backups'
        self.lock = threading.RLock()

    def ready(self):
        # Never silently place backups in the container when the mount is absent.
        if not self.root.is_dir() or self.root.is_symlink() or not (self.root / '.mungoon-backups').is_file():
            raise BackupError('The NAS backup folder is not connected yet.', 503)
        if not os.access(self.root, os.W_OK):
            raise BackupError('The NAS backup folder is not writable.', 503)
        return self.root

    def safe_path(self, *parts):
        path = self.root
        for part in parts:
            path = path / part
            if path.is_symlink(): raise BackupError('Unsafe storage path.', 409)
        if not path.resolve().is_relative_to(self.root.resolve()): raise BackupError('Unsafe storage path.', 409)
        return path

    def load(self, ident):
        if not re.fullmatch(r'[a-f0-9]{32}', ident): raise BackupError('Invalid upload identifier.')
        p = self.meta / (ident + '.json')
        if not p.is_file(): raise BackupError('Upload not found.', 404)
        return json.loads(p.read_text(encoding='utf-8'))

    def store(self, info):
        self.meta.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest = self.meta / (info['id'] + '.json'); temp = dest.with_suffix('.tmp')
        with open(temp, 'w', encoding='utf-8') as f:
            os.chmod(temp, 0o600); json.dump(info, f, ensure_ascii=True); f.flush(); os.fsync(f.fileno())
        os.replace(temp, dest)
        backup_sync_directory(self.meta)

    def public(self, info):
        result = {k: info[k] for k in ('id', 'name', 'size', 'created', 'folder', 'state')}
        if info['state'] == 'complete':
            target = self.safe_path(info['folder'], info['name'])
            result.update(offset=info['size'], sha256=info['sha256'], present=target.is_file(), path=BACKUP_SHARE + '\\' + info['folder'] + '\\' + info['name'])
        else:
            part = self.safe_path('.incoming', info['id'] + '.part')
            length = part.stat().st_size if part.exists() else 0
            # A process interruption can leave a partial last chunk. Never acknowledge it.
            result['offset'] = length if length == info['size'] else min(info['size'], length // BACKUP_CHUNK * BACKUP_CHUNK)
        return result

    def status(self):
        with self.lock:
            self.ready()
            rows = []
            if self.meta.exists():
                for p in sorted(self.meta.glob('*.json'), key=lambda p:p.stat().st_mtime, reverse=True)[:100]:
                    rows.append(self.public(json.loads(p.read_text(encoding='utf-8'))))
            return {'available':True, 'share':BACKUP_SHARE, 'chunkSize':BACKUP_CHUNK, 'maxFileSize':BACKUP_MAX, 'free':shutil.disk_usage(self.root).free, 'uploads':rows}

    def create(self, value):
        if not isinstance(value, dict) or set(value) != {'name','size','modified','hashes'}: raise BackupError('Invalid upload manifest.')
        name, size, modified, hashes = (value[k] for k in ('name','size','modified','hashes'))
        if not isinstance(name, str) or not name or len(name.encode('utf-8')) > 220 or name != unicodedata.normalize('NFC', name) or name[-1] in '. ' or name.startswith('.') or re.search(r'[\x00-\x1f\x7f/\\:*?"<>|]', name) or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name, re.I):
            raise BackupError('Use a normal filename without slashes, leading dots or Windows-reserved characters.')
        if type(size) is not int or not 0 <= size <= BACKUP_MAX or type(modified) is not int or not 0 <= modified < 10**16: raise BackupError('Invalid file size or date.')
        if not isinstance(hashes, list) or len(hashes) != (size + BACKUP_CHUNK - 1) // BACKUP_CHUNK or any(not isinstance(h,str) or not re.fullmatch(r'[a-f0-9]{64}',h) for h in hashes): raise BackupError('Invalid chunk checksums.')
        ident = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',',':')).encode()).hexdigest()[:32]
        with self.lock:
            self.ready()
            if (self.meta / (ident + '.json')).exists(): return self.public(self.load(ident))
            if shutil.disk_usage(self.root).free < size + 64 * 1024**2: raise BackupError('Not enough free space on the NAS.', 507)
            incoming = self.safe_path('.incoming'); incoming.mkdir(exist_ok=True, mode=0o700)
            part = self.safe_path('.incoming', ident + '.part')
            if part.exists(): raise BackupError('Unrecorded partial file needs owner review.', 409)
            with open(part, 'xb') as f:
                os.chmod(part, 0o600); f.flush(); os.fsync(f.fileno())
            backup_sync_directory(incoming)
            info = dict(value, id=ident, created=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), folder=time.strftime('%Y-%m-%d', time.gmtime())+'_'+ident[:12], state='uploading')
            self.store(info)
            return self.public(info)

    def chunk(self, ident, offset, body):
        with self.lock:
            self.ready(); info = self.load(ident)
            if type(offset) is not int or offset < 0 or offset % BACKUP_CHUNK or offset >= info['size']: raise BackupError('Invalid chunk offset.')
            expected = min(BACKUP_CHUNK, info['size'] - offset)
            if len(body) != expected or hashlib.sha256(body).hexdigest() != info['hashes'][offset // BACKUP_CHUNK]: raise BackupError('Chunk checksum failed; reselect the original file.', 422)
            state = self.public(info)
            if info['state'] == 'complete': return state
            if offset > state['offset']: raise BackupError('An earlier chunk is missing. Resume from the saved position.', 409)
            part = self.safe_path('.incoming', ident + '.part')
            with open(part, 'r+b') as f:
                if offset < state['offset']:
                    f.seek(offset)
                    if f.read(expected) != body: raise BackupError('Saved chunk changed. Owner review is required.',409)
                    return state
                f.truncate(offset); f.seek(offset); f.write(body); f.flush(); os.fsync(f.fileno())
            return self.public(info)

    def complete(self, ident):
        with self.lock:
            self.ready(); info = self.load(ident)
            if info['state'] == 'complete': return self.public(info)
            folder = self.safe_path(info['folder']); target = self.safe_path(info['folder'], info['name'])
            part = self.safe_path('.incoming', ident + '.part')
            # Recover if rename succeeded immediately before the metadata commit.
            source = part if part.is_file() else target
            if not source.is_file() or source.stat().st_size != info['size']: raise BackupError('The upload is incomplete.', 409)
            digest = hashlib.sha256()
            with open(source, 'rb') as f:
                for expected in info['hashes']:
                    block = f.read(BACKUP_CHUNK)
                    if hashlib.sha256(block).hexdigest() != expected: raise BackupError('Stored bytes failed verification.', 422)
                    digest.update(block)
                if f.read(1): raise BackupError('Unexpected stored bytes.', 422)
            folder.mkdir(exist_ok=True, mode=0o770)
            os.chmod(folder, 0o770)
            if source == part:
                if target.exists(): raise BackupError('A file already exists at this destination. Nothing was overwritten.', 409)
                os.chmod(part, 0o660); os.rename(part, target)
                backup_sync_directory(folder); backup_sync_directory(part.parent); backup_sync_directory(self.root)
            info.update(state='complete', sha256=digest.hexdigest()); self.store(info)
            return self.public(info)

def qr_backups_request(handler, backups):
    """Called only after the gateway's owner authorization."""
    try:
        if not handler.identity.get('owner'): raise BackupError('Only the owner can use NAS backups.',403)
        path = urlparse(handler.path).path
        if handler.command == 'GET':
            if path == '/api/admin/qr-backups': return handler.send_json(backups.status())
            raise BackupError('Not found.',404)
        if not handler.origin_ok(): return handler.reject_origin()
        if handler.headers.get('Transfer-Encoding'): raise BackupError('Chunked requests are not supported.',400)
        length = int(handler.headers.get('Content-Length','-1'))
        if not 0 < length <= BACKUP_CHUNK: raise BackupError('Request exceeds the 1 MiB chunk limit.',413)
        handler.connection.settimeout(45)
        body = handler.rfile.read(length)
        if len(body) != length: raise BackupError('Incomplete request.',400)
        match = re.fullmatch(r'/api/admin/qr-backups/([a-f0-9]{32})/(chunk|complete)', path)
        if match and match[2] == 'chunk':
            if handler.headers.get('Content-Type','').split(';')[0] != 'application/octet-stream': raise BackupError('Expected a binary chunk.',415)
            result = backups.chunk(match[1], int(handler.headers.get('X-Upload-Offset','-1')), body)
        else:
            if handler.headers.get('Content-Type','').split(';')[0] != 'application/json': raise BackupError('Expected JSON.',415)
            value = json.loads(body)
            if path == '/api/admin/qr-backups': result = backups.create(value)
            elif match and match[2] == 'complete' and value == {}: result = backups.complete(match[1])
            else: raise BackupError('Not found.',404)
        return handler.send_json(result)
    except BackupError as e: return handler.send_json({'error':str(e)}, e.status)
    except (ValueError, TypeError): return handler.send_json({'error':'Invalid backup request.'},400)
    except Exception: return handler.send_json({'error':'NAS storage is unavailable. Your acknowledged chunks are retained; retry shortly.'},503)
