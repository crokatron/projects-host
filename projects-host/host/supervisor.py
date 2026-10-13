"""Run the editor service and accept signed releases on a separate LAN port."""
import base64, hashlib, importlib.util, io, json, os, signal, subprocess, sys, tarfile, threading, time, urllib.request
from pathlib import Path, PurePosixPath
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

HOME = Path('/srv')
RELEASES = HOME/'releases'
PUBLIC_KEY = Ed25519PublicKey.from_public_bytes(bytes.fromhex((HOME/'deploy-public.hex').read_text().strip()))
DEPLOY_LOCK = threading.Lock()
CHILD = None

def current(): return (HOME/'current').resolve()
sys.path.insert(0,str(current()))
def history_api():
    spec=importlib.util.spec_from_file_location('active_tool_history',current()/'history.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module
def record(path):return history_api().record(path)
def prepare_restore(*args):return history_api().prepare_restore(*args)
def atomic(*args):return history_api().atomic(*args)
def start(path):
    return subprocess.Popen([sys.executable, str(path/'server.py')], cwd=path, env={**os.environ,'PROJECT_DATA':str(HOME/'data')})
def stop(child):
    child.terminate()
    try: child.wait(20)
    except subprocess.TimeoutExpired: child.kill(); child.wait()
def health(release=None):
    try:
        with urllib.request.urlopen('http://127.0.0.1:9070/health',timeout=2) as r: data=json.load(r)
        return data if not release or data['release']==release else None
    except Exception: return None
def point(path):
    temp=HOME/'next'
    temp.unlink(missing_ok=True)
    temp.symlink_to(path)
    os.replace(temp,HOME/'current')

def install(body):
    global CHILD
    digest=hashlib.sha256(body).hexdigest()
    target=RELEASES/digest[:20]
    if target.exists(): raise ValueError('Release already uploaded; publish a new version')
    target.mkdir()
    total=0
    with tarfile.open(fileobj=io.BytesIO(body),mode='r:gz') as archive:
        members=archive.getmembers()
        if len(members)>1000: raise ValueError('Too many files')
        seen=set()
        for member in members:
            p=PurePosixPath(member.name)
            if not member.isfile() or p.is_absolute() or '..' in p.parts or '\\' in member.name or member.name in seen: raise ValueError('Unsafe archive')
            seen.add(member.name)
            total+=member.size
            if total>100000000: raise ValueError('Expanded release too large')
            if p.parts[0] not in {'public','lampshade','seeds','server.py','history.py','release.json'}: raise ValueError('Unexpected release file')
            dest=target.joinpath(*p.parts)
            dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_bytes(archive.extractfile(member).read())
    manifest=json.loads((target/'release.json').read_text())
    for rel,digest in manifest['files'].items():
        if rel not in seen or hashlib.sha256((target/rel).read_bytes()).hexdigest()!=digest: raise ValueError('Manifest mismatch')
    if seen != set(manifest['files'])|{'release.json'}: raise ValueError('Unexpected files')
    manifest=history_api().apply_overrides(target)
    for p in target.rglob('*.py'): compile(p.read_text(),str(p),'exec')
    # Import the CAD engine before stopping the good release.
    checked=subprocess.run([sys.executable,'-c','import server'],cwd=target,capture_output=True,timeout=45)
    if checked.returncode: raise ValueError('Release import validation failed')
    status=health()
    if status and status.get('exportBusy'): raise ValueError('CAD export in progress; retry after it finishes')
    return activate(target,manifest)

def activate(target,manifest):
    global CHILD
    previous=current()
    stop(CHILD)
    point(target)
    CHILD=start(target)
    for _ in range(40):
        if health(manifest['id']):
            record(target)
            return manifest
        if CHILD.poll() is not None: break
        time.sleep(1)
    stop(CHILD)
    point(previous)
    CHILD=start(previous)
    raise ValueError('New release failed health check; previous release restored')

class Deployment(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        if self.path!='/deploy': return self.send_error(404)
        if not DEPLOY_LOCK.acquire(blocking=False): return self.send_error(409)
        try:
            n=int(self.headers.get('Content-Length','0'))
            if not 0<n<=32000000: raise ValueError('Invalid archive size')
            stamp=self.headers.get('X-Deploy-Time','')
            if abs(time.time()-int(stamp))>300: raise ValueError('Expired deployment')
            self.connection.settimeout(60)
            body=self.rfile.read(n)
            if len(body)!=n: raise ValueError('Incomplete upload')
            message=(stamp+'\n'+hashlib.sha256(body).hexdigest()).encode()
            PUBLIC_KEY.verify(base64.b64decode(self.headers.get('X-Deploy-Signature',''),validate=True),message)
            manifest=install(body)
            response=json.dumps({'ok':True,'release':manifest['id']}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(response)));self.end_headers();self.wfile.write(response)
        except Exception as error:
            msg=str(error) if isinstance(error,ValueError) else 'Deployment rejected'
            response=json.dumps({'ok':False,'error':msg}).encode()
            self.send_response(400);self.send_header('Content-Length',str(len(response)));self.end_headers();self.wfile.write(response)
        finally: DEPLOY_LOCK.release()

if __name__=='__main__':
    record(current())
    CHILD=start(current())
    threading.Thread(target=ThreadingHTTPServer(('0.0.0.0',9071),Deployment).serve_forever,daemon=True).start()
    def shutdown(*_): stop(CHILD);sys.exit(0)
    signal.signal(signal.SIGTERM,shutdown)
    while True:
        time.sleep(3)
        request=HOME/'restore-request.json'
        if request.exists():
            with DEPLOY_LOCK:
                job=json.loads(request.read_text());request.unlink()
                try:
                    status=health()
                    if status and status.get('exportBusy'):raise ValueError('CAD export in progress; retry later')
                    target,manifest=prepare_restore(job['tool'],job['version'])
                    checked=subprocess.run([sys.executable,'-c','import server'],cwd=target,capture_output=True,timeout=45)
                    if checked.returncode:raise ValueError('Historical runtime validation failed')
                    activate(target,manifest)
                    history_api().restore_committed(job['tool'],target)
                    atomic(HOME/'restore-status.json',{'state':'complete','tool':job['tool'],'version':job['version']})
                except Exception as e:
                    atomic(HOME/'restore-status.json',{'state':'failed','error':str(e)})
        if CHILD.poll() is not None:
            with DEPLOY_LOCK:
                if CHILD.poll() is not None: CHILD=start(current())
        studio=HOME/'studio-request.json'
        if studio.exists():
            with DEPLOY_LOCK:
                job=json.loads(studio.read_text());studio.unlink()
                try:
                    status=health()
                    if status and status.get('exportBusy'):raise ValueError('CAD export in progress; retry after it finishes.')
                    target,manifest=history_api().prepare_studio(job)
                    checked=subprocess.run([sys.executable,'-c','import server'],cwd=target,capture_output=True,timeout=45)
                    if checked.returncode:raise ValueError('Project runtime validation failed.')
                    activate(target,manifest)
                    history_api().studio_committed(job)
                    atomic(HOME/'studio-status.json',{'state':'complete','workspace':job['id'],'release':manifest['id']})
                except Exception as e:atomic(HOME/'studio-status.json',{'state':'failed','error':str(e)[:1000]})
