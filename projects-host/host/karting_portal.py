"""Real karting results, imported from the user's workbook; no simulator data."""
import hashlib, io, json, math, os, threading, zipfile, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

def portal_id(*values):
    return hashlib.sha256(json.dumps(values,ensure_ascii=False).encode()).hexdigest()[:24]

def portal_xlsx(raw):
    ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        infos=z.infolist()
        if len(infos)>300 or sum(i.file_size for i in infos)>40_000_000 or any(i.file_size>15_000_000 for i in infos):raise ValueError('Workbook is too large when expanded.')
        def xml(name):
            b=z.read(name)
            if b'<!DOCTYPE' in b.upper() or b'<!ENTITY' in b.upper():raise ValueError('Unsupported XML declarations.')
            return ET.fromstring(b)
        strings=[]
        if 'xl/sharedStrings.xml' in z.namelist():
            strings=[''.join(n.itertext()) for n in xml('xl/sharedStrings.xml').findall('s:si',ns)]
        rels={r.attrib['Id']:r.attrib['Target'] for r in xml('xl/_rels/workbook.xml.rels')}
        sheets={}
        for sheet in xml('xl/workbook.xml').findall('s:sheets/s:sheet',ns):
            title=sheet.attrib['name']
            if title not in ('All Sessions','Every Lap','Participants','About'):continue
            target=rels[sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']]
            target=target.lstrip('/') if target.startswith('/') else 'xl/'+target
            if '..' in target.split('/'):raise ValueError('Invalid worksheet path.')
            rows=[]
            for row in xml(target).findall('s:sheetData/s:row',ns):
                cells={}
                for c in row.findall('s:c',ns):
                    ref=c.attrib.get('r','');letters=''.join(x for x in ref if x.isalpha());col=0
                    for char in letters:col=col*26+ord(char.upper())-64
                    if not 1<=col<=40:raise ValueError('Unsupported worksheet width.')
                    v=c.find('s:v',ns);value=v.text if v is not None else ''
                    if c.attrib.get('t')=='s':value=strings[int(value)]
                    elif c.attrib.get('t')=='inlineStr':value=''.join(c.find('s:is',ns).itertext()) if c.find('s:is',ns) is not None else ''
                    elif c.attrib.get('t')=='e':raise ValueError('The workbook contains a cell error.')
                    cells[col-1]=value or ''
                if cells:rows.append([cells.get(i,'') for i in range(max(cells)+1)])
            if rows:sheets[title]=[dict(zip(rows[0],r+['']*(len(rows[0])-len(r)))) for r in rows[1:] if any(r)]
        return sheets

def portal_parse(raw):
    try:sheets=portal_xlsx(raw)
    except (zipfile.BadZipFile,KeyError,IndexError,ET.ParseError,RuntimeError) as e:raise ValueError('Cannot read this .xlsx archive. Use the karting archive workbook format.') from e
    if not all(k in sheets for k in ('All Sessions','Every Lap','Participants')):raise ValueError('Workbook needs All Sessions, Every Lap and Participants sheets.')
    def text(v,label):
        v=str(v or '').strip()
        if not v or len(v)>250:raise ValueError('Missing or overly long '+label+'.')
        return v
    def number(v,label,integer=False,optional=False):
        if v in ('',None) and optional:return None
        try:n=float(v)
        except (TypeError,ValueError):raise ValueError('Invalid '+label+'.')
        if not math.isfinite(n) or n<0 or n>100000 or (integer and n!=int(n)):raise ValueError('Invalid '+label+'.')
        if not integer and n<=0:raise ValueError('Lap times must be positive.')
        return int(n) if integer else round(n,3)
    participants={};sessions={};entries={};laps={};warnings=[]
    for row in sheets['Participants']:
        name=text(row.get('Participant'),'participant');key=name.lower();ident=portal_id(key)
        decision=row.get('Decision','Review') or 'Review'
        if decision not in ('Review','Tentative','Keep','Remove'):raise ValueError('Unknown participant decision.')
        if decision=='Review':decision='Tentative'
        if ident in participants:raise ValueError('Duplicate participant names in Participants sheet.')
        participants[ident]=dict(id=ident,name=name,decision=decision)
    def common(row):
        name=text(row.get('Participant'),'participant');driver=portal_id(name.lower())
        if driver not in participants:raise ValueError('Participant is missing from Participants sheet: '+name)
        venue=text(row.get('Venue'),'venue');day=text(row.get('Date'),'date')
        try:day=date.fromisoformat(day).isoformat()
        except ValueError:
            try:day=(date(1899,12,30)+timedelta(days=int(float(day)))).isoformat()
            except (ValueError,OverflowError):raise ValueError('Dates must use YYYY-MM-DD or Excel date cells.')
        label=text(row.get('Session'),'session');source=row.get('Email Source','')
        # An opaque source key distinguishes same-day reports without publishing mailbox URLs.
        sid=portal_id(venue,day,label,source)
        sessions[sid]=dict(id=sid,venue=venue,date=day,name=label)
        return driver,sid
    for row in sheets['All Sessions']:
        driver,sid=common(row);key=portal_id(driver,sid)
        item=dict(id=key,driver=driver,session=sid,reportedBest=number(row.get('Best (seconds)'),'best lap',optional=True),reportedLaps=number(row.get('Laps Logged'),'lap count',True,True))
        if key in entries and entries[key]!=item:raise ValueError('Conflicting participant-session rows.')
        entries[key]=item
    for row in sheets['Every Lap']:
        driver,sid=common(row);lap=number(row.get('Lap No.'),'lap number',True)
        if lap<1:raise ValueError('Lap numbering must start at one.')
        key=portal_id(driver,sid,lap);item=dict(id=key,driver=driver,session=sid,lap=lap,seconds=number(row.get('Lap (seconds)'),'lap time'))
        if key in laps and laps[key]!=item:raise ValueError('Conflicting times for the same driver and lap.')
        laps[key]=item
        if portal_id(driver,sid) not in entries:raise ValueError('Every Lap contains a driver/session missing from All Sessions.')
    if not entries or not laps:raise ValueError('The workbook has no usable timing records.')
    if len(laps)>50000 or len(entries)>5000 or len(participants)>2000:raise ValueError('Archive exceeds supported limits.')
    grouped={}
    for lap in laps.values():grouped.setdefault((lap['driver'],lap['session']),[]).append(lap['seconds'])
    for entry in entries.values():
        times=grouped.get((entry['driver'],entry['session']),[])
        if times and entry['reportedBest'] is not None and abs(min(times)-entry['reportedBest'])>.0011:warnings.append(dict(entry=entry['id'],message='Reported best differs from the minimum recorded lap.'))
        if entry['reportedLaps'] is not None and len(times)!=entry['reportedLaps']:warnings.append(dict(entry=entry['id'],message='Recorded lap count differs from the session summary.'))
    return dict(participants=list(participants.values()),sessions=list(sessions.values()),entries=list(entries.values()),laps=list(laps.values()),warnings=warnings)

class KartingPortal:
    def __init__(self,data):self.folder=Path(data);self.path=self.folder/'karting-portal.json';self.lock=threading.RLock()
    def read(self):
        doc=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else dict(version=1,participants=[],sessions=[],entries=[],laps=[],warnings=[],imports=[],notes={})
        doc.setdefault('excluded',{})
        doc.setdefault('profiles',{})
        doc.setdefault('merges',{})
        doc.setdefault('reference',{})
        doc.setdefault('sharing',{'live':False})
        for person in doc['participants']:
            if person.get('decision')=='Review':person['decision']='Tentative'
        return doc
    def revision(self,doc):return '"'+hashlib.sha256(json.dumps(doc,sort_keys=True,separators=(',',':')).encode()).hexdigest()+'"'
    def save(self,doc):
        self.folder.mkdir(parents=True,exist_ok=True);tmp=self.path.with_suffix('.tmp')
        with tmp.open('w',encoding='utf-8') as f:json.dump(doc,f,ensure_ascii=False,allow_nan=False);f.flush();os.fsync(f.fileno())
        os.replace(tmp,self.path)
    def import_book(self,raw,revision,name,preview=False):
        incoming=portal_parse(raw);fingerprint=hashlib.sha256(raw).hexdigest()
        with self.lock:
            doc=self.read()
            if revision!=self.revision(doc):raise FileExistsError('Results changed. Refresh before importing again.')
            if any(i['hash']==fingerprint for i in doc['imports']):return doc,dict(duplicate=True)
            added={}
            for field in ('participants','sessions','entries','laps'):
                previous={r['id']:r for r in doc[field]};added[field]=0
                for row in incoming[field]:
                    if row['id'] in previous:
                        if field=='participants':continue # NAS review choices are authoritative.
                        if previous[row['id']]!=row:raise ValueError('Conflicting existing '+field+' data. Existing results were kept; import a corrected new session or ask for a targeted correction.')
                    else:previous[row['id']]=row;added[field]+=1
                doc[field]=list(previous.values())
            if len(doc['laps'])>50000 or len(doc['imports'])>=100:raise ValueError('Archive limit reached.')
            if preview:return doc,dict(added=added,warnings=incoming['warnings'])
            doc['warnings']+= [w for w in incoming['warnings'] if w not in doc['warnings']]
            doc['imports'].append(dict(hash=fingerprint,name=Path(name.replace('\\','/')).name[:150],at=datetime.now(timezone.utc).isoformat(),added=added))
            originals=self.folder/'karting-workbooks';originals.mkdir(parents=True,exist_ok=True)
            dest=originals/(fingerprint+'.xlsx')
            if not dest.exists():dest.write_bytes(raw)
            self.save(doc);return doc,dict(added=added)
    def update(self,value,revision):
        if not isinstance(value,dict):raise ValueError('Expected an update object.')
        with self.lock:
            doc=self.read()
            if revision!=self.revision(doc):raise FileExistsError('Results changed. Refresh before saving again.')
            people={p['id']:p for p in doc['participants']}
            def root(ident):
                seen=set()
                while ident in doc['merges']:
                    if ident in seen:raise ValueError('Driver merge cycle needs repair.')
                    seen.add(ident);ident=doc['merges'][ident]
                return ident
            def driver():
                ident=value.get('driver')
                if ident not in people:raise ValueError('Choose an existing driver.')
                return ident
            if value.get('action')=='sharing':
                live=value.get('live')
                if type(live) is not bool:raise ValueError('Choose Go live or Take offline.')
                if live:portal_enable_public_edge(self.folder)
                doc['sharing']={'live':live}
            elif value.get('action')=='bulk-drivers':
                chosen=value.get('drivers');operation=value.get('operation')
                if not isinstance(chosen,list) or not 1<=len(chosen)<=1000 or any(not isinstance(x,str) or x not in people for x in chosen):raise ValueError('Select existing driver profiles.')
                selected={root(x) for x in chosen}
                if operation not in ('Keep','Tentative','remove','restore','delete'):raise ValueError('Choose a driver action.')
                if operation=='delete':
                    if value.get('confirmActive') is not True and any(not doc['profiles'].get(x,{}).get('hidden') for x in selected):raise ValueError('Preview and confirm deletion of active profiles first.')
                    removed={x for x in people if root(x) in selected}
                    lapids={x['id'] for x in doc['laps'] if x['driver'] in removed}
                    entryids={x['id'] for x in doc['entries'] if x['driver'] in removed}
                    doc['participants']=[x for x in doc['participants'] if x['id'] not in removed]
                    doc['entries']=[x for x in doc['entries'] if x['id'] not in entryids]
                    doc['laps']=[x for x in doc['laps'] if x['id'] not in lapids]
                    doc['warnings']=[x for x in doc['warnings'] if x['entry'] not in entryids]
                    for field in ('excluded','reference'):
                        doc[field]={k:v for k,v in doc[field].items() if k not in lapids}
                    doc['profiles']={k:v for k,v in doc['profiles'].items() if k not in removed}
                    doc['merges']={k:v for k,v in doc['merges'].items() if k not in removed and v not in removed}
                else:
                    for ident in selected:
                        if operation in ('remove','restore'):doc['profiles'].setdefault(ident,{})['hidden']=operation=='remove'
                        else:people[ident]['decision']=operation
            elif value.get('action')=='summary-import':
                incoming=value.get('sessions')
                if not isinstance(incoming,list) or not 1<=len(incoming)<=20:raise ValueError('Invalid summary sessions.')
                for s in incoming:
                    day=date.fromisoformat(s['date']).isoformat();venue=s['venue'];name=s['name'];source=s['source']
                    if any(not isinstance(x,str) or not 1<=len(x)<=200 for x in (venue,name,source)):raise ValueError('Invalid summary labels.')
                    sid=portal_id('summary',venue,day,name,source)
                    session=dict(id=sid,date=day,venue=venue,name=name,timingDetail='summary',source=source)
                    existing=next((x for x in doc['sessions'] if x['id']==sid),None)
                    if existing and existing!=session:raise ValueError('Summary session conflicts with existing data.')
                    if not existing:doc['sessions'].append(session)
                    if not isinstance(s.get('drivers'),list) or not 1<=len(s['drivers'])<=100:raise ValueError('Invalid summary drivers.')
                    for r in s['drivers']:
                        driver_name=r['name']
                        if not isinstance(driver_name,str) or not 1<=len(driver_name.strip())<=120:raise ValueError('Invalid driver name.')
                        did=portal_id(driver_name.strip().lower())
                        if did not in people:
                            people[did]=dict(id=did,name=driver_name.strip(),decision='Tentative');doc['participants'].append(people[did])
                        for field in ('best','second'):
                            if type(r[field]) not in (int,float) or not math.isfinite(r[field]) or not 0<r[field]<10000:raise ValueError('Invalid summary timing.')
                        if r['second']<r['best']:raise ValueError('Second-best time is faster than best.')
                        for field in ('bestLap','secondLap','rank','kart'):
                            if type(r[field]) is not int or not 1<=r[field]<=10000:raise ValueError('Invalid printed lap/rank/kart number.')
                        entry=dict(id=portal_id(did,sid),driver=did,session=sid,reportedBest=r['best'],reportedLaps=None,summaryOnly=True,bestLap=r['bestLap'],secondBest=r['second'],secondLap=r['secondLap'],sourceRank=r['rank'],kart=r['kart'])
                        prior=next((e for e in doc['entries'] if e['id']==entry['id']),None)
                        if prior and prior!=entry:raise ValueError('Summary result conflicts with existing timing.')
                        if not prior:doc['entries'].append(entry)
            elif value.get('action')=='profile':
                ident=root(driver());name=value.get('actualName','')
                if not isinstance(name,str) or len(name.strip())>120:raise ValueError('Actual name must be at most 120 characters.')
                doc['profiles'].setdefault(ident,{})['actualName']=name.strip()
            elif value.get('action')=='visibility':
                ident=root(driver());hidden=value.get('hidden')
                if type(hidden) is not bool:raise ValueError('Choose remove or restore.')
                doc['profiles'].setdefault(ident,{})['hidden']=hidden
            elif value.get('action')=='merge-many':
                target=root(driver());sources=value.get('sources')
                name=value.get('actualName')
                if 'actualName' in value and (not isinstance(name,str) or not 1<=len(name.strip())<=120):raise ValueError('Enter a driver name of 1 to 120 characters.')
                if not isinstance(sources,list) or not 1<=len(sources)<=100 or any(not isinstance(x,str) or x not in people for x in sources):raise ValueError('Choose names to merge into this profile.')
                roots={root(x) for x in sources}
                if target in roots:raise ValueError('The main profile cannot also be a source.')
                occupied={e['session'] for e in doc['entries'] if root(e['driver'])==target}
                for ident in roots:
                    sessions={e['session'] for e in doc['entries'] if root(e['driver'])==ident}
                    if sessions&occupied:raise ValueError('Selected profiles overlap in a session. No names were merged; review those results first.')
                    occupied.update(sessions)
                for ident in roots:doc['merges'][ident]=target
                if name is not None:doc['profiles'].setdefault(target,{})['actualName']=name.strip()
            elif value.get('action')=='merge':
                source=root(driver());target=value.get('target')
                if target not in people:raise ValueError('Choose a target driver.')
                target=root(target)
                if source==target:raise ValueError('These names are already one driver.')
                sessions=lambda ident:{e['session'] for e in doc['entries'] if root(e['driver'])==ident}
                if sessions(source)&sessions(target):raise ValueError('Both drivers have results in the same session. Merge blocked to avoid combining two different racers.')
                doc['merges'][source]=target
            elif value.get('action')=='unmerge':
                ident=driver()
                if ident not in doc['merges']:raise ValueError('This source name is not merged.')
                del doc['merges'][ident]
            elif value.get('action')=='reference':
                rows=value.get('laps');label=value.get('source')
                if not isinstance(rows,list) or not 1<=len(rows)<=5000 or not isinstance(label,str) or not 1<=len(label)<=150:raise ValueError('Invalid reference analysis.')
                laps={l['id']:l for l in doc['laps']}
                for item in rows:
                    lap=laps.get(item.get('id'));trend=item.get('trend');status=item.get('status')
                    if not lap or item.get('seconds')!=lap['seconds'] or type(trend) not in (int,float) or not math.isfinite(trend) or not 0<trend<100000 or status not in ('clean','incident','pit','cooldown'):raise ValueError('Reference must match an existing lap and valid trend/status.')
                    incoming=dict(trend=trend,status=status,source=label)
                    if lap['id'] in doc['reference'] and doc['reference'][lap['id']]!=incoming:raise ValueError('Existing reference analysis differs. No data was replaced.')
                    doc['reference'][lap['id']]=incoming
            elif value.get('action')=='decision':
                decision=value.get('decision');driver=next((p for p in doc['participants'] if p['id']==value.get('driver')),None)
                if decision not in ('Review','Tentative','Keep','Remove') or not driver:raise ValueError('Choose a participant and review decision.')
                driver['decision']='Tentative' if decision=='Review' else decision
            elif value.get('action')=='lap':
                ident=value.get('lap');exclude=value.get('exclude');reason=value.get('reason','Incident')
                if ident not in {l['id'] for l in doc['laps']} or type(exclude) is not bool or not isinstance(reason,str) or len(reason)>250:raise ValueError('Choose a valid lap and an incident note of up to 250 characters.')
                if exclude:doc['excluded'][ident]=dict(reason=reason.strip() or 'Incident',at=datetime.now(timezone.utc).isoformat())
                else:doc['excluded'].pop(ident,None)
            elif value.get('action')=='note':
                sid=value.get('session');note=value.get('note')
                if sid not in {s['id'] for s in doc['sessions']} or not isinstance(note,str) or len(note)>4000:raise ValueError('Choose a session and a note of up to 4,000 characters.')
                doc['notes'][sid]=note
            else:raise ValueError('Unknown update.')
            self.save(doc);return doc

def karting_portal_request(handler,portal):
    path=handler.path.split('?')[0]
    try:
        if handler.command=='GET' and path.endswith('/stats'):
            with portal.lock:doc=portal.read();revision=portal.revision(doc)
            return handler.send_json({**doc,'owner':handler.identity['owner']},etag=revision)
        if handler.command!='POST' or path not in ('/yas-karting/api/stats','/yas-karting/api/import','/yas-karting/api/import-preview'):return handler.send_json({'error':'Method not allowed.'},405)
        if not handler.identity['owner']:return handler.send_json({'error':'Only the owner can import results or change shared review notes.'},403)
        if not handler.origin_ok():return handler.reject_origin()
        expected='application/json' if path.endswith('/stats') else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        if handler.headers.get('Content-Type','').split(';')[0]!=expected:return handler.send_json({'error':'Unsupported upload format.'},415)
        n=int(handler.headers.get('Content-Length','0'))
        if not 1<=n<=10_000_000 or handler.headers.get('Transfer-Encoding'):return handler.send_json({'error':'Upload must be at most 10 MB.'},413)
        raw=handler.rfile.read(n);revision=handler.headers.get('If-Match')
        if path.endswith(('/import','/import-preview')):
            doc,summary=portal.import_book(raw,revision,handler.headers.get('X-File-Name','Karting archive.xlsx'),path.endswith('/import-preview'))
            if path.endswith('/import-preview'):return handler.send_json(summary)
        else:doc=portal.update(json.loads(raw),revision)
        return handler.send_json({**doc,'owner':True},etag=portal.revision(doc))
    except FileExistsError as e:return handler.send_json({'error':str(e)},409)
    except (ValueError,TypeError,UnicodeError) as e:return handler.send_json({'error':str(e)},400)
    except (BrokenPipeError,ConnectionAbortedError,ConnectionResetError):return
    except OSError:return handler.send_json({'error':'NAS storage is unavailable. Reload to check the archive before retrying.'},503)

PORTAL_PUBLIC_DOMAIN='projects.mungoon.net/karting'
PORTAL_PUBLIC_ASSETS={'index.html','style.css','app.js','analysis.js','ratings.js','ratings-ui.js','drivers.js'}

def portal_enable_public_edge(folder,transport=None):
    """Provision one public path only; never modify the protected Projects application."""
    account='d41f3e6bde0f8696db58508e5210183d';base='/accounts/'+account+'/access/apps'
    def request(method,path,value=None):
        try:
            token=json.loads((Path(folder)/'private/seerr-cloudflare.json').read_text())['token']
            req=urllib.request.Request('https://api.cloudflare.com/client/v4'+path,data=json.dumps(value).encode() if value is not None else None,method=method,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=15) as response:result=json.load(response)
            if result.get('success') is not True or result.get('result_info',{}).get('total_pages',1)>1:raise ValueError()
            return result['result']
        except Exception:raise ValueError('Public link setup could not be verified with Cloudflare. The portal has not been made public; check the saved Cloudflare connection.') from None
    request=transport or request
    apps=request('GET',base+'?per_page=200')
    matches=[a for a in apps if a.get('domain')==PORTAL_PUBLIC_DOMAIN]
    if len(matches)>1:raise ValueError('More than one public Karting application exists. Review before publishing.')
    if not matches:
        app=request('POST',base,{'name':'Mungoon Karting public results','type':'self_hosted','domain':PORTAL_PUBLIC_DOMAIN,'app_launcher_visible':False,'session_duration':'24h','policies':[{'name':'Public Karting read-only route','decision':'bypass','include':[{'everyone':{}}],'exclude':[],'require':[],'precedence':1}]})
    else:app=matches[0]
    if app.get('domain')!=PORTAL_PUBLIC_DOMAIN or app.get('type')!='self_hosted':raise ValueError('Unexpected public application scope.')
    policies=request('GET',base+'/'+app['id']+'/policies')
    if len(policies)!=1 or policies[0].get('decision')!='bypass' or policies[0].get('include')!=[{'everyone':{}}] or policies[0].get('exclude') or policies[0].get('require'):raise ValueError('Public path policy differs from the expected scope. No NAS sharing setting changed.')

def portal_public_document(doc):
    def root(ident):
        seen=set()
        while ident in doc.get('merges',{}):
            if ident in seen:raise ValueError('Invalid merge')
            seen.add(ident);ident=doc['merges'][ident]
        return ident
    people=[dict(id=p['id'],name=doc.get('profiles',{}).get(p['id'],{}).get('actualName') or p['name'],decision='Keep' if p.get('decision')=='Keep' else 'Tentative') for p in doc['participants'] if root(p['id'])==p['id'] and not doc.get('profiles',{}).get(p['id'],{}).get('hidden')]
    ids={p['id'] for p in people};entries=[{**e,'driver':root(e['driver'])} for e in doc['entries'] if root(e['driver']) in ids];laps=[{**l,'driver':root(l['driver'])} for l in doc['laps'] if root(l['driver']) in ids];lapids={l['id'] for l in laps};entryids={e['id'] for e in entries};sessions={e['session'] for e in entries}
    return dict(version=doc['version'],owner=False,public=True,participants=people,sessions=[s for s in doc['sessions'] if s['id'] in sessions],entries=entries,laps=laps,warnings=[w for w in doc['warnings'] if w['entry'] in entryids],excluded={k:{'reason':'Incident'} for k in doc['excluded'] if k in lapids},reference={k:v for k,v in doc['reference'].items() if k in lapids},notes={},imports=[],profiles={},merges={},sharing={'live':True})

def karting_public_request(handler,portal,root):
    path=handler.path.split('?')[0]
    if handler.command!='GET':return handler.send_json({'error':'Public Karting is read-only.'},405)
    try:
        with portal.lock:doc=portal.read()
        if not doc['sharing'].get('live'):return handler.send_json({'error':'This Karting portal is offline.'},404)
        if path=='/karting':
            handler.send_response(308);handler.send_header('Location','/karting/');handler.end_headers();return
        if path=='/karting/api/stats':return handler.send_json(portal_public_document(doc))
        if path=='/karting/brand.png':return handler.send_file(Path(root)/'public/brand.png')
        name=path.removeprefix('/karting/') or 'index.html'
        if name not in PORTAL_PUBLIC_ASSETS:return handler.send_json({'error':'Not found.'},404)
        if name!='index.html':return handler.send_file(Path(root)/'public/yas-karting'/name)
        raw=(Path(root)/'public/yas-karting/index.html').read_text(encoding='utf-8').replace('src="/brand.png"','src="/karting/brand.png"').replace('href="/" aria-label="Mungoon projects"','href="/karting/" aria-label="Mungoon Karting"')
        raw=raw.replace('</head>','<link rel="icon" href="/karting/brand.png"><meta name="robots" content="noindex"></head>');data=raw.encode()
        handler.send_response(200);handler.send_header('Content-Type','text/html;charset=utf-8');handler.send_header('Content-Length',str(len(data)));handler.send_header('Cache-Control','no-store');handler.end_headers();handler.wfile.write(data)
    except (OSError,ValueError):return handler.send_json({'error':'Karting results are unavailable.'},503)
