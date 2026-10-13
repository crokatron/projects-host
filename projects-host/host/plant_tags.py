"""Owner-only plant tag catalog and revision-protected print settings."""
import hashlib,json,os,re,threading,urllib.request
from pathlib import Path
from home_assistant import HA_URL

PLANT_TAG_LOCK=threading.Lock()
PLANT_TAG_FIELDS=set('style width base relief stakeLength stakeWidth stakeThickness construction reinforcement clearance flagSide flagShape informationLayout rotation fontSize textMode labelWidth labelHeight nozzle ecc backColor qrColor textColor'.split())
def plant_tag_catalog(ha):
 request=urllib.request.Request(HA_URL+'/api/states/sensor.mungoon_plant_map',headers={'Authorization':'Bearer '+ha.token()})
 with urllib.request.urlopen(request,timeout=20) as response:value=json.load(response)
 attrs=value['attributes']
 return {'updated_at':attrs.get('updated_at'),'plants':[{k:p.get(k) for k in ('id','name','scientific_name','record_url','ha_url','qr_url')} for p in attrs['plants']]}

def plant_tag_read(folder,pid):
 path=Path(folder)/'plant-tags'/f'{pid}.json'
 body=path.read_bytes() if path.exists() else b'{}'
 return json.loads(body),'"'+hashlib.sha256(body).hexdigest()+'"'

def plant_tag_save(folder,pid,settings,expected):
 if not isinstance(settings,dict) or set(settings)-PLANT_TAG_FIELDS:raise ValueError('Only print settings can be saved. Plant names and links follow Plant Manager.')
 if len(json.dumps(settings))>12000:raise ValueError('Print settings are too large.')
 with PLANT_TAG_LOCK:
  old,etag=plant_tag_read(folder,pid)
  if expected!=etag:raise FileExistsError('This tag changed elsewhere. Reload before saving.')
  path=Path(folder)/'plant-tags'/f'{pid}.json';path.parent.mkdir(exist_ok=True)
  if path.exists():
   history=path.parent/'history';history.mkdir(exist_ok=True)
   (history/f'{pid}-{etag.strip(chr(34))}.json').write_bytes(path.read_bytes())
  temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(settings,sort_keys=True),encoding='utf-8');os.replace(temporary,path)
  return plant_tag_read(folder,pid)

def plant_tags_request(handler,ha,folder):
 try:
  path=handler.path.split('?')[0]
  is_default=path=='/api/admin/plant-tags/defaults'
  catalog={'plants':[]} if is_default else plant_tag_catalog(ha)
  if path=='/api/admin/plant-tags' and handler.command=='GET':return handler.send_json(catalog)
  match=re.fullmatch(r'/api/admin/plant-tags/([1-9][0-9]*)',path)
  if not match and not is_default:return handler.send_json({'error':'Not found'},404)
  pid='defaults' if is_default else int(match[1]);plant=next((p for p in catalog['plants'] if p['id']==pid),None)
  if not plant and not is_default:return handler.send_json({'error':'Plant is no longer in Plant Manager.'},404)
  if handler.command=='PUT':
   if not handler.origin_ok():return handler.reject_origin()
   if handler.headers.get('Content-Type','').split(';')[0]!='application/json':return handler.send_json({'error':'JSON required'},415)
   size=int(handler.headers.get('Content-Length','0'))
   if not 0<size<=12000:raise ValueError('Invalid settings size.')
   settings=json.loads(handler.rfile.read(size))
   if is_default and (not isinstance(settings,dict) or settings.get('style')!='flag'):raise ValueError('The default must be a flag design.')
   value,etag=plant_tag_save(folder,pid,settings,handler.headers.get('If-Match'))
  else:value,etag=plant_tag_read(folder,pid)
  return handler.send_json({'plant':plant,'settings':value},etag=etag)
 except FileExistsError as exc:return handler.send_json({'error':str(exc)},409)
 except (ValueError,TypeError) as exc:return handler.send_json({'error':str(exc)},400)
 except Exception:return handler.send_json({'error':'Plant collection is unavailable. Check the Plants dashboard in HA and retry.'},503)
