"""NAS project gateway. Only curated public files are served; state is separate."""
import hashlib, json, mimetypes, os, sys, threading, time
import io, posixpath
from access import AccessControl, PROJECTS
from seerr_access import SeerrAccess, SeerrAccessError
from studio import Studio, studio_bridge
from home_assistant import HomeAssistantSync, HAError, ha_validate
from ha_automations import AutomationStudio, AutomationConflict
from building_notices import BuildingNotices, building_notices_request
from branding import branded_html, BRAND_ASSETS
from qr_backups import QRBackups, qr_backups_request
from plant_tags import plant_tags_request
from pathlib import Path
from urllib.parse import urlparse, unquote
from http.server import ThreadingHTTPServer

# BEGIN IMAGE REVIEW MODULE
from image_review import ImageReview, image_review_request
# END IMAGE REVIEW MODULE
from karting import KartingStore, karting_request, karting_public_request

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('PROJECT_DATA', '/srv/data'))
DATA.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / 'lampshade'))
from server_lamp import Handler as LampHandler, CAD_LOCK

LOCK = threading.RLock()
ORIGINS = {'http://192.168.0.54:9070', 'https://projects.mungoon.net', 'http://127.0.0.1:9070'}
STATES = {'floor-plan': 'floor-plan.json', 'garmin': 'garmin.json', 'lampshade': 'lampshade.json'}
ACCESS = AccessControl(DATA)
QR_BACKUPS = QRBackups(DATA)
KARTING = KartingStore(DATA)
IMAGE_REVIEW = ImageReview(DATA)
SEERR = SeerrAccess(DATA)
STUDIO = Studio(ROOT, DATA)
HA = HomeAssistantSync(DATA)
AUTOMATIONS = AutomationStudio(DATA, HA)
BUILDING_NOTICES = BuildingNotices(DATA, HA)

def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()

def read_state(name):
    target = DATA / (name + '.json')
    seed = ROOT / 'seeds' / STATES.get(name, 'absent.json')
    value = json.loads(target.read_text()) if target.exists() else json.loads(seed.read_text()) if seed.exists() else None
    return value, '"' + hashlib.sha256(encoded(value)).hexdigest() + '"'

def save_state(name, value):
    target = DATA / (name + '.json')
    if target.exists():
        backup = DATA / 'history' / name
        backup.mkdir(parents=True, exist_ok=True)
        # One recovery point per minute, in addition to the current atomic file.
        dest = backup / (time.strftime('%Y%m%d-%H%M') + '.json')
        if not dest.exists(): dest.write_bytes(target.read_bytes())
    temp = target.with_suffix('.tmp')
    temp.write_bytes(encoded(value))
    os.replace(temp, target)
    return read_state(name)[1]

class Handler(LampHandler):
    server_version = 'MungoonProjects'
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        # Never log cookies, signed URLs, request bodies or deployment credentials.
        sys.stderr.write('%s %s %s\n' % (self.command, urlparse(self.path).path, args[1] if len(args)>1 else ''))

    def end_headers(self):
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
        super().end_headers()

    def deny_access(self, message, status):
        p = urlparse(self.path).path
        if self.command == 'GET' and '/api/' not in p and (p == '/' or p.endswith(('/', '.html'))):
            import html
            body = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                '<title>Project access · Mungoon</title><body style="background:#111817;color:#edf2e9;'
                'font:17px/1.6 system-ui;margin:12vh auto;padding:24px;max-width:620px">'
                '<p style="color:#d6f48b">MUNGOON / PROJECTS</p><h1>Access is not enabled.</h1><p>'
                + html.escape(message) + '</p><p>If you used the wrong email, sign out and try again.</p>'
                '<a style="color:#d6f48b" href="/cdn-cgi/access/logout">Sign out</a></body>').encode()
            self.send_response(status); self.send_header('Content-Type', 'text/html;charset=utf-8')
            self.send_header('Content-Length', str(len(body))); self.send_header('Cache-Control', 'no-store')
            self.end_headers(); self.wfile.write(body)
        else: self.send_json({'error': message}, status)

    def authorize(self):
        raw = urlparse(self.path).path
        p = unquote(raw)
        # Reject aliases before permission checks and file resolution.
        if p != raw or '\\' in p or '\x00' in p or not p.startswith('/') or p.startswith('//') or posixpath.normpath(p) != p.rstrip('/') and p != '/':
            self.send_error(400); return False
        try:
            if self.headers.get('X-Project-Signature'):
                # Existing owner deployment key, request-bound signature, never a browser credential.
                if self.headers.get('Host') not in ('192.168.0.54:9070', '127.0.0.1:9070'):
                    raise ValueError('Local endpoint required')
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 <= n <= 2000000 or self.headers.get('Transfer-Encoding'): raise ValueError('Invalid body')
                self.connection.settimeout(20)
                body = self.rfile.read(n)
                if len(body) != n: raise ValueError('Incomplete body')
                email = ACCESS.verify_local(self.command, self.path, self.headers, body)
                self.rfile = io.BytesIO(body)
            else:
                token = self.headers.get('Cf-Access-Jwt-Assertion')
                if not token:
                    if self.command == 'GET' and (p == '/' or p.endswith(('/', '.html'))):
                        self.send_response(303)
                        self.send_header('Location', 'https://projects.mungoon.net' + p)
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                    else: self.send_json({'error': 'Sign in at https://projects.mungoon.net'}, 401)
                    return False
                email = ACCESS.verify_jwt(token)
            self.identity = ACCESS.identity(email)
        except PermissionError:
            self.deny_access('Ask the owner to allow your email address and select the projects you can use.', 403); return False
        except Exception:
            self.deny_access('Login could not be verified. Sign in again or try shortly.', 401); return False
        project = p.split('/')[1]
        if p.startswith('/api/designs/'): project = p.rsplit('/', 1)[-1]
        if p in ('/floor-plan-preview.png','/floor-plan-preview.svg'): project = 'floor-plan'
        if project in PROJECTS:
            allowed = project in self.identity['projects']
        elif p in ('/', '/index.html', '/api/me'):
            allowed = True
        else:
            allowed = self.identity['owner']
        if not allowed:
            self.deny_access('You do not have access to this project. Ask the owner to enable it.', 403)
        return allowed

    def do_HEAD(self):
        # Do not inherit SimpleHTTPRequestHandler's ungated file handler.
        self.send_response(405); self.send_header('Allow', 'GET, PUT, POST'); self.end_headers()

    def converter_proxy(self):
        import http.client, re
        path=urlparse(self.path).path.removeprefix('/stl-converter')
        allowed=(self.command=='POST' and path=='/api/convert') or (self.command=='GET' and (path=='/api/samples' or re.fullmatch(r'/api/jobs/[a-f0-9]{12}(?:/(?:solid.glb|mesh.glb|download-all|download/(?:step|brep|iges)))?',path)))
        if not allowed:return self.send_error(404)
        if self.command=='POST' and not self.origin_ok():return self.reject_origin()
        try:
            n=int(self.headers.get('Content-Length','0'))
            if not 0<=n<=33*1024*1024 or self.headers.get('Transfer-Encoding'):return self.send_error(413)
            if self.command=='POST' and not self.headers.get('Content-Type','').startswith('multipart/form-data;'):return self.send_error(415)
            self.connection.settimeout(45)
            body=self.rfile.read(n)
            if len(body)!=n:return self.send_error(400)
            upstream=http.client.HTTPConnection('mungoon-stl-converter',8765,timeout=50)
            try:
                upstream.request(self.command,path,body,{'Content-Type':self.headers.get('Content-Type','application/octet-stream'),'X-Project-Email':self.identity['email']})
                response=upstream.getresponse();data=response.read(100000001)
                if len(data)>100000000:raise ValueError()
                self.send_response(response.status)
                self.send_header('Content-Type',response.getheader('Content-Type','application/octet-stream'))
                disposition=response.getheader('Content-Disposition')
                if disposition:self.send_header('Content-Disposition',disposition)
                self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)
            finally:upstream.close()
        except Exception:return self.send_json({'detail':'The NAS converter is unavailable. Please retry shortly.'},503)

    def send_json(self, value, status=200, etag=None):
        data = encoded(value)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if etag:
            self.send_header('ETag', etag)
            # Proxies may weaken the HTTP ETag after compression; this is the
            # canonical application revision used for optimistic draft writes.
            self.send_header('X-Project-Revision', etag)
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, file):
        if not file.is_file(): return self.send_error(404)
        data = file.read_bytes()
        offline_name={'sender.html':'File-Sender.html','receiver.html':'File-Receiver.html','sender-python.zip':'File-Sender-Python.zip','sender-powershell.zip':'File-Sender-Windows.zip'}.get(file.name) if file.parent==(ROOT/'public/qr-transfer').resolve() else None
        if file.suffix.lower()=='.html' and not offline_name:data=branded_html(data)
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream' if offline_name else (mimetypes.guess_type(file.name)[0] or 'application/octet-stream'))
        if offline_name:self.send_header('Content-Disposition',f'attachment; filename="{offline_name}"')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.end_headers()
        self.wfile.write(data)

    def state_name(self):
        p = urlparse(self.path).path
        if p == '/garmin/api/draft': return 'garmin'
        if p == '/garmin/api/request': return 'garmin-request'
        if p.startswith('/api/designs/') and p.split('/')[-1] in STATES: return p.split('/')[-1]
        return None

    def do_GET(self):
        p = urlparse(self.path).path
        if p=='/karting' or p.startswith('/karting/'):return karting_public_request(self,KARTING.portal,ROOT)
        # Only fixed, non-sensitive logo files are public. Never serve an arbitrary path here.
        if p in BRAND_ASSETS: return self.send_file(ROOT/'public'/BRAND_ASSETS[p])
        if p == '/health':
            return self.send_json({'ok': True, 'release': json.loads((ROOT/'release.json').read_text())['id'], 'exportBusy': CAD_LOCK.locked()})
        if not self.authorize(): return
        if self.path.startswith('/yas-karting/api/'): return karting_request(self, KARTING, ACCESS)
        if p.startswith('/image-review/api/'): return image_review_request(self, IMAGE_REVIEW)
        if p == '/image-review':
            self.send_response(308); self.send_header('Location','/image-review/'); self.end_headers(); return
        if p.startswith('/api/admin/qr-backups'): return qr_backups_request(self, QR_BACKUPS)
        if p.startswith('/api/admin/plant-tags'): return plant_tags_request(self, HA, DATA)
        if p == '/api/admin/ha/building-notices': return building_notices_request(self, BUILDING_NOTICES)
        if p == '/api/admin/ha/status': return self.send_json(HA.status())
        if p == '/api/admin/ha/device-sheet': return self.send_json(HA.sheet_reference())
        if p == '/api/admin/ha/buttons':
            try:return self.send_json(AUTOMATIONS.buttons())
            except HAError as error:return self.send_json({'error':str(error)},424)
            except Exception:return self.send_json({'error':'Could not read button presses from HA.'},502)
        if p == '/api/admin/ha/automations':
            try:return self.send_json(AUTOMATIONS.catalog())
            except HAError as error:return self.send_json({'error':str(error)},424)
            except Exception:return self.send_json({'error':'Could not read Home Assistant automations.'},502)
        if p == '/api/admin/ha/automation-options':
            try:return self.send_json(AUTOMATIONS.options())
            except HAError as error:return self.send_json({'error':str(error)},424)
            except Exception:return self.send_json({'error':'Could not read Home Assistant automation options.'},502)
        if p == '/api/admin/ha/map-context':
            try:return self.send_json(HA.map_context())
            except HAError as error:return self.send_json({'error':str(error)},424)
            except Exception:return self.send_json({'error':'Could not read HA home location.'},502)
        if p == '/api/admin/ha/inventory':
            try: return self.send_json(HA.inventory())
            except HAError as error: return self.send_json({'error':str(error)},424)
            except Exception: return self.send_json({'error':'Home Assistant is unavailable. Refresh devices to retry.'},502)
        if p.startswith('/stl-converter/api/'):return self.converter_proxy()
        if p.startswith('/api/studio/'):
            try:
                from urllib.parse import parse_qs
                query=parse_qs(urlparse(self.path).query)
                if p=='/api/studio/status':
                    value=studio_bridge('/status');value['workspaces']=STUDIO.listing()
                    status=DATA.parent/'studio-status.json';value['publish']=json.loads(status.read_text()) if status.exists() else None
                    return self.send_json(value)
                if p=='/api/studio/events':return self.send_json(studio_bridge('/events?after='+str(max(0,int(query.get('after',['0'])[0])))))
                sid=query.get('session',[''])[0];STUDIO.paths(sid)
                if p=='/api/studio/review':return self.send_json(STUDIO.review(sid))
                if p=='/api/studio/history':return self.send_json(studio_bridge('/history?session='+sid))
                return self.send_error(404)
            except ValueError as e:return self.send_json({'error':str(e)},400)
        if p == '/api/admin/runtime-status':
            if not self.headers.get('X-Project-Signature'): return self.send_error(403)
            result={}
            for name in ('converter-status','codex-status','nas-status'):
                file=DATA/'runtime-packages'/(name+'.txt')
                if file.is_file(): result[name]=file.read_text(errors='replace')[-32000:]
            return self.send_json(result)
        if p == '/api/me': return self.send_json(self.identity)
        if p == '/api/admin/seerr':
            try:
                value, etag = SEERR.status()
                return self.send_json(value, etag=etag)
            except SeerrAccessError as error: return self.send_json({'error': str(error)}, 424)
            except Exception: return self.send_json({'error': 'Seerr access could not be loaded. Refresh or reconnect Cloudflare.'}, 502)
        if p == '/api/admin/users':
            value, etag = ACCESS.users()
            return self.send_json(value, etag=etag)
        if p == '/api/history':
            from history import catalogue, version
            rows=catalogue()
            for slug,versions in rows.items():
                active=version(ROOT,slug)[0]
                for row in versions:
                    row.pop('files',None);row.pop('release',None);row['active']=row['version']==active
            status=Path('/srv/restore-status.json')
            return self.send_json({'tools':rows,'restore':json.loads(status.read_text()) if status.exists() else None})
        name = self.state_name()
        if name:
            with LOCK: value, etag = read_state(name)
            return self.send_json(value, etag=etag)
        if p == '/garmin/api/proposal':
            with LOCK: value, etag = read_state('garmin-proposal')
            return self.send_json(value, etag=etag)
        if p.startswith('/lampshade/api/'):
            self.path = self.path[len('/lampshade'):]
            return super().do_GET()
        if p in {'/'+slug for slug in PROJECTS}:
            self.send_response(308); self.send_header('Location', p+'/'); self.end_headers(); return
        relative = unquote(p).lstrip('/')
        if not relative or relative.endswith('/'): relative += 'index.html'
        public = (ROOT/'public').resolve()
        candidate = (public/relative).resolve()
        if not candidate.is_relative_to(public) or any(x.startswith('.') for x in Path(relative).parts): return self.send_error(404)
        return self.send_file(candidate)

    def origin_ok(self):
        return self.headers.get('Origin') in ORIGINS

    def reject_origin(self):
        # Consume a bounded body before replying so HTTP/1.0 closes cleanly.
        try:
            n=int(self.headers.get('Content-Length','0'))
            if 0<n<=2000000:self.rfile.read(n)
        except ValueError:pass
        return self.send_json({'error':'Origin not allowed'},403)

    def do_PUT(self):
        if not self.authorize(): return
        if self.path.startswith('/api/admin/plant-tags'): return plant_tags_request(self, HA, DATA)
        name = self.state_name()
        if not name: return self.send_error(404)
        if not self.origin_ok(): return self.reject_origin()
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2000000: return self.send_error(413)
            if self.headers.get('Content-Type','').split(';')[0] != 'application/json': return self.send_error(415)
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict): raise ValueError('Expected a design object')
            if name == 'floor-plan':
                ha_validate(value)
                if HA.status()['connected'] and not self.identity['owner']:
                    return self.send_json({'error':'This floor plan is connected to HA. Only the owner can save changes to it.'},403)
            if name == 'garmin' and (value.get('device') != 'fenix9pro47mm' or value.get('version') != 1): raise ValueError('Invalid watch design')
            if name == 'lampshade' and value.get('app') != 'lampshade-studio': raise ValueError('Invalid lampshade design')
            with LOCK:
                _, current = read_state(name)
                if name != 'garmin-request' and self.headers.get('If-Match') != current:
                    return self.send_json({'error':'A newer design is saved on the NAS. Export your work, then reload before saving.'},409)
                etag = save_state(name, value)
                sync = HA.enqueue(value, etag) if name == 'floor-plan' and self.identity['owner'] else None
            return self.send_json({'saved':True, **({'ha':sync} if sync is not None else {})},etag=etag)
        except (ValueError, TypeError): return self.send_json({'error':'Invalid JSON or design'},400)

    def do_POST(self):
        if self.path.split('?')[0]=='/karting' or self.path.startswith('/karting/'):return karting_public_request(self,KARTING.portal,ROOT)
        if not self.authorize(): return
        if self.path.startswith('/yas-karting/api/'): return karting_request(self, KARTING, ACCESS)
        if self.path.startswith('/image-review/api/'): return image_review_request(self, IMAGE_REVIEW)
        if self.path.startswith('/api/admin/qr-backups'): return qr_backups_request(self, QR_BACKUPS)
        if self.path in ('/api/admin/ha/building-notices/setup','/api/admin/ha/building-notices/event','/api/admin/ha/building-notices/notify-current','/api/admin/ha/building-notices/google-prepare'): return building_notices_request(self, BUILDING_NOTICES)
        if self.path.startswith('/api/admin/ha/'):
            if not self.origin_ok(): return self.reject_origin()
            try:
                n=int(self.headers.get('Content-Length','0'))
                limit=500000 if self.path=='/api/admin/ha/device-sheet' else 100000 if self.path=='/api/admin/ha/automations' else 10000
                if not 0<n<=limit or self.headers.get('Transfer-Encoding'): raise ValueError('Invalid request size.')
                if self.headers.get('Content-Type','').split(';')[0]!='application/json': return self.send_error(415)
                value=json.loads(self.rfile.read(n))
                if self.path=='/api/admin/ha/device-sheet': return self.send_json(HA.import_sheet_reference(value))
                if self.path=='/api/admin/ha/automations': return self.send_json(AUTOMATIONS.save(value))
                if self.path=='/api/admin/ha/buttons': return self.send_json(AUTOMATIONS.save_button(value))
                if self.path=='/api/admin/ha/connect': return self.send_json(HA.begin_auth(self.identity['email']))
                if self.path=='/api/admin/ha/complete':
                    HA.finish_auth(self.identity['email'],value.get('state'),value.get('code'))
                elif self.path!='/api/admin/ha/sync': return self.send_error(404)
                with LOCK:
                    plan,etag=read_state('floor-plan')
                    result=HA.enqueue(plan,etag)
                return self.send_json(result)
            except AutomationConflict as error:return self.send_json({'error':str(error)},409)
            except HAError as error: return self.send_json({'error':str(error)},424)
            except (ValueError,TypeError) as error: return self.send_json({'error':str(error) if self.path in ('/api/admin/ha/automations','/api/admin/ha/buttons') else 'Invalid HA connection request.'},400)
            except Exception: return self.send_json({'error':'HA connection failed. Please retry.'},502)
        if self.path.startswith('/stl-converter/api/'):return self.converter_proxy()
        if self.path.startswith('/api/studio/'):
            if not self.origin_ok():return self.reject_origin()
            try:
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=100000 or self.headers.get('Transfer-Encoding'):raise ValueError('Invalid request.')
                value=json.loads(self.rfile.read(n))
                route=self.path.removeprefix('/api/studio')
                if route=='/workspace':return self.send_json(STUDIO.create(value['project']))
                if route=='/login':return self.send_json(studio_bridge('/login',{}))
                if route=='/answer':return self.send_json(studio_bridge('/answer',value))
                STUDIO.paths(value['session'])
                if route in ('/message','/stop'):return self.send_json(studio_bridge(route,value))
                if route=='/publish':return self.send_json(STUDIO.publish(value['session'],value['revision']),202)
                return self.send_error(404)
            except (ValueError,KeyError,TypeError) as e:return self.send_json({'error':str(e)},400)
        if self.path == '/api/admin/runtime-package':
            if not self.headers.get('X-Project-Signature') or not self.origin_ok(): return self.send_error(403)
            try:
                import base64
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=1500000: raise ValueError()
                value=json.loads(self.rfile.read(n))
                if set(value)!={'name','data','sha256'} or value['name'] not in ('converter-bootstrap.tar.gz','codex-bootstrap.tar.gz','runtime-update.tar.gz'): raise ValueError()
                body=base64.b64decode(value['data'],validate=True)
                if not body.startswith(b'\x1f\x8b') or hashlib.sha256(body).hexdigest()!=value['sha256']: raise ValueError()
                folder=DATA/'runtime-packages';folder.mkdir(exist_ok=True,mode=0o700)
                target=folder/value['name'];temp=target.with_suffix('.tmp')
                temp.write_bytes(body);os.chmod(temp,0o600);os.replace(temp,target)
                return self.send_json({'staged':value['name'],'sha256':value['sha256']})
            except (ValueError,KeyError,TypeError): return self.send_json({'error':'Invalid runtime package.'},400)
        if self.path in ('/api/admin/seerr', '/api/admin/seerr/connect'):
            if not self.origin_ok(): return self.reject_origin()
            # A token may only be submitted through the public HTTPS site or an
            # authenticated, request-signed LAN operation, never a LAN browser.
            if self.path.endswith('/connect') and self.headers.get('Origin') != 'https://projects.mungoon.net' and not self.headers.get('X-Project-Signature'):
                return self.send_json({'error': 'Open https://projects.mungoon.net to connect securely.'}, 403)
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 < n <= 4096 or self.headers.get('Transfer-Encoding'): raise ValueError('Invalid request size.')
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json': return self.send_error(415)
                value = json.loads(self.rfile.read(n))
                if not isinstance(value, dict): raise ValueError('Invalid request.')
                if self.path.endswith('/connect'):
                    if set(value) != {'token'}: raise ValueError('Enter a Cloudflare API token.')
                    result, etag = SEERR.connect(value['token'])
                else: result, etag = SEERR.update(value, self.headers.get('If-Match'), self.identity['email'])
                return self.send_json(result, etag=etag)
            except FileExistsError as error: return self.send_json({'error': str(error)}, 409)
            except (ValueError, TypeError): return self.send_json({'error': 'Check the email or connection details. Your owner email cannot be removed.'}, 400)
            except SeerrAccessError as error: return self.send_json({'error': str(error)}, 424)
            except Exception: return self.send_json({'error': 'The operation could not be completed. Refresh the list before retrying.'}, 502)
        if self.path == '/api/admin/users':
            if not self.origin_ok(): return self.reject_origin()
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 < n <= 8192: raise ValueError('Invalid request size.')
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json': return self.send_error(415)
                value = json.loads(self.rfile.read(n))
                if not isinstance(value, dict): raise ValueError('Invalid user settings.')
                result, etag = ACCESS.update(value, self.headers.get('If-Match'), self.identity['email'])
                return self.send_json(result, etag=etag)
            except FileExistsError as error: return self.send_json({'error': str(error)}, 409)
            except (ValueError, TypeError) as error: return self.send_json({'error': str(error)}, 400)
        if self.path=='/api/history/restore':
            if not self.origin_ok():return self.reject_origin()
            from history import catalogue, atomic
            try:
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<1000:raise ValueError('Invalid request')
                job=json.loads(self.rfile.read(n));slug=job['tool'];ver=job['version']
                if not any(r['version']==ver for r in catalogue().get(slug,[])):raise ValueError('Version not found')
                with LOCK:
                    task=Path('/srv/restore-request.json')
                    status=Path('/srv/restore-status.json')
                    if task.exists() or (status.exists() and json.loads(status.read_text()).get('state')=='pending'):return self.send_json({'error':'A restore is already running'},409)
                    atomic(status,{'state':'pending','tool':slug,'version':ver})
                    atomic(task,{'tool':slug,'version':ver})
                return self.send_json({'queued':True},202)
            except (ValueError,KeyError,TypeError):return self.send_json({'error':'Invalid restore request'},400)
        if not self.path.startswith('/lampshade/api/'): return self.send_error(404)
        if not self.origin_ok(): return self.reject_origin()
        self.path = self.path[len('/lampshade'):]
        # The proxy has validated the exact public or LAN origin. The old local-only
        # handler's HTTP-host check is bypassed without weakening its body validation.
        del self.headers['Origin']
        return super().do_POST()

if __name__ == '__main__':
    if HA.status()['connected']:
        with LOCK:
            plan,etag=read_state('floor-plan')
            HA.enqueue(plan,etag)
    HA.start(LOCK, read_state, save_state)
    ThreadingHTTPServer(('0.0.0.0', 9070), Handler).serve_forever()
