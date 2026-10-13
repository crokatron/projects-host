"""Append-only, project-authorized observations; separate from guide and GPS data."""
import json, math, os, threading, uuid, time, secrets
from access import mailbox_key, OWNER_EMAIL
from karting_portal import KartingPortal, karting_portal_request, karting_public_request
from datetime import date, datetime, timezone
from pathlib import Path

class KartingStore:
    def __init__(self, data):
        self.path = Path(data) / 'yas-karting-feedback.json'
        self.lock = threading.RLock()
        self.races = KartingRooms(leaderboard=KartingLeaderboard(Path(data)/'yas-karting-leaderboard.json'))
        self.portal = KartingPortal(data)

    def listing(self):
        with self.lock:
            return json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else []

    def append(self, value, author):
        if not isinstance(value, dict): raise ValueError('Expected an observation.')
        try: ident = str(uuid.UUID(value.get('id', '')))
        except (ValueError, TypeError, AttributeError): raise ValueError('Invalid observation ID.')
        if type(value.get('turn')) is not int or not 1 <= value['turn'] <= 12: raise ValueError('Choose a turn from 1 to 12.')
        if value.get('surface') not in ('Dry', 'Wet', 'Drying'): raise ValueError('Choose the surface condition.')
        if value.get('change') not in ('Line / apex', 'Braking / throttle', 'Grip / standing water', 'Map / layout mismatch'): raise ValueError('Choose a feedback category.')
        if value.get('guide') != 'yas-2024-clockwise-v1': raise ValueError('Unknown guide version.')
        try: session_date = date.fromisoformat(value.get('sessionDate', '')).isoformat()
        except (ValueError, TypeError): raise ValueError('Enter a session date.')
        note = value.get('observation')
        if not isinstance(note, str) or not 1 <= len(note.strip()) <= 2000: raise ValueError('Enter an observation of up to 2,000 characters.')
        sketch = value.get('sketch', [])
        if not isinstance(sketch, list) or len(sketch) > 400: raise ValueError('Sketch is too long.')
        for point in sketch:
            if not isinstance(point, list) or len(point) != 2 or any(type(x) not in (int, float) or not math.isfinite(x) for x in point) or not (0 <= point[0] <= 780 and 0 <= point[1] <= 561): raise ValueError('Invalid sketch coordinates.')
        item = dict(id=ident, turn=value['turn'], surface=value['surface'], change=value['change'], guide=value['guide'], sessionDate=session_date, observation=note.strip(), sketch=sketch, author=author)
        with self.lock:
            items = self.listing()
            for existing in items:
                if existing['id'] == ident:
                    if {k:v for k,v in existing.items() if k != 'created'} != item: raise ValueError('This ID was already used for a different observation.')
                    return existing
            if len(items) >= 5000: raise ValueError('Feedback archive is full. Ask the owner to archive it before adding more.')
            item['created'] = datetime.now(timezone.utc).isoformat()
            items.append(item)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            with temp.open('w', encoding='utf-8') as stream:
                json.dump(items, stream, ensure_ascii=False, allow_nan=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, self.path)
            return item

def karting_request(handler, store, access=None):
    if handler.path.split('?')[0] in ('/yas-karting/api/stats','/yas-karting/api/import','/yas-karting/api/import-preview'):
        return karting_portal_request(handler, store.portal)
    if handler.path.split("?")[0] != "/yas-karting/api/feedback":
        return handler.send_json({'error':'The game and simulator have been retired. Open Karting for real session results.'},410)
    if handler.path.split('?')[0] != '/yas-karting/api/feedback': return handler.send_json({'error':'Unknown karting endpoint.'},404)
    try:
        if handler.command == 'GET':
            # Observations are shared within the project's existing allowlist; omit account identifiers.
            return handler.send_json({'items':[{k:v for k,v in item.items() if k != 'author'} for item in store.listing()]})
        if handler.command != 'POST': return handler.send_json({'error':'Method not allowed.'},405)
        if not handler.origin_ok(): return handler.reject_origin()
        if handler.headers.get('Content-Type','').split(';')[0] != 'application/json': return handler.send_json({'error':'Send JSON.'},415)
        n = int(handler.headers.get('Content-Length','0'))
        if not 1 <= n <= 50000 or handler.headers.get('Transfer-Encoding'): return handler.send_json({'error':'Observation is too large.'},413)
        value = json.loads(handler.rfile.read(n))
        item = store.append(value, handler.identity['email'])
        return handler.send_json({'saved':True,'id':item['id']})
    except (ValueError, TypeError, UnicodeError): return handler.send_json({'error':'Invalid observation. Check the date, turn, text and sketch, then retry.'},400)
    except OSError: return handler.send_json({'error':'NAS storage is unavailable. Keep your draft and retry.'},503)


KART_RULES = 'paddock-2026-10-v2'
KART_COLORS = ['#c3f47c','#ff8cba','#72d8ff','#ffd166','#b8a1ff','#ff9966','#63efc4','#ffffff']
KART_PHYSICS = 'smooth-kart-v2'

class KartingLeaderboard:
    """Personal bests only; identities stay private, and timings come from room gates."""
    def __init__(self,path):
        self.path=Path(path);self.lock=threading.RLock()

    def read(self):
        if not self.path.exists():return []
        records=json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(records,list):raise ValueError('Leaderboard storage needs repair.')
        return records

    @staticmethod
    def category(options,kind,laps):
        values={k:float(options[k]) for k in ('power','maxSpeed','brakes','grip')}
        values.update(wet=options['wet'],tyres=options['tyres'])
        return json.dumps([KART_PHYSICS,kind,laps,values],sort_keys=True,separators=(',',':'))

    def record(self,email,name,options,kind,laps,seconds):
        if not math.isfinite(seconds) or seconds<=0:raise ValueError('Invalid server timing.')
        category=self.category(options,kind,laps);email=mailbox_key(email);seconds=round(seconds,1)
        with self.lock:
            records=self.read();existing=next((r for r in records if r['email']==email and r['category']==category),None)
            if existing and existing['seconds']<=seconds:return
            if not existing and len(records)>=5000:raise ValueError('Leaderboard storage is full.')
            driver=next((r['driver'] for r in records if r['email']==email),uuid.uuid4().hex)
            item=dict(email=email,driver=driver,name=name,category=category,options=dict(options),kind=kind,laps=laps,seconds=seconds,physics=KART_PHYSICS,at=datetime.now(timezone.utc).isoformat())
            records=[r for r in records if not (r['email']==email and r['category']==category)]+[item]
            self.path.parent.mkdir(parents=True,exist_ok=True);temp=self.path.with_suffix('.tmp')
            with temp.open('w',encoding='utf-8') as stream:
                json.dump(records,stream,ensure_ascii=False,allow_nan=False);stream.flush();os.fsync(stream.fileno())
            os.replace(temp,self.path)

    def listing(self,email):
        with self.lock:records=self.read()
        groups={}
        for record in records:
            if record['physics']!=KART_PHYSICS:continue
            group=groups.setdefault(record['category'],dict(id=record['category'],options=record['options'],kind=record['kind'],laps=record['laps'],entries=[]))
            group['entries'].append({k:record[k] for k in ('driver','name','seconds','at')}|{'you':record['email']==mailbox_key(email)})
        for group in groups.values():
            group['entries'].sort(key=lambda r:(r['seconds'],r['at'],r['driver']))
            rank=0;previous=None
            for i,row in enumerate(group['entries']):
                if row['seconds']!=previous:rank=i+1
                row['rank']=rank;previous=row['seconds']
            group['entries']=group['entries'][:100]
        return dict(groups=list(groups.values()),physics=KART_PHYSICS)

class KartingRooms:
    """Small private, non-contact races. Identity, room controls and lap gates live on NAS.

    Browser vehicle movement is checked for bounds/jumps, not competitive anti-cheat.
    Rooms expire and are deliberately not persisted across a server restart.
    """
    def __init__(self, route=None, clock=None, leaderboard=None):
        self.lock = threading.RLock()
        self.rooms = {}
        self.clock = clock or time.time
        self.route = route
        self.leaderboard=leaderboard;self.pending=[];self.leaderboard_warning=None

    def flush_results(self):
        if not self.leaderboard:return
        try:
            while self.pending:
                self.leaderboard.record(**self.pending[0]);self.pending.pop(0)
            self.leaderboard_warning=None
        except (OSError,ValueError):self.leaderboard_warning='Leaderboard save pending. Results will retry while this NAS session stays online.'

    def queue_result(self,room,player,kind,seconds):
        if self.leaderboard:
            item=dict(email=player['email'],name=player['name'],options=dict(room['options']),kind=kind,laps=room['raceLaps'] if kind=='race' else 1,seconds=seconds)
            existing=next((r for r in self.pending if r['email']==item['email'] and r['kind']==kind and r['laps']==item['laps'] and r['options']==item['options']),None)
            if existing:
                if seconds<existing['seconds']:existing.update(item)
            else:self.pending.append(item)

    def geometry(self):
        if self.route is None:
            root = Path(__file__).resolve().parent
            path = root/'public/yas-karting/race-route.json'
            if not path.exists(): path = root.parent/'site/yas-karting/race-route.json'
            self.route = json.loads(path.read_text())
        return self.route

    def sweep(self, access):
        now = self.clock()
        # One permission read per poll, regardless of the number of active rooms.
        # Match the central store's fail-closed handling of duplicate aliases.
        with access.lock: users=access.document()['users']
        grouped={}
        for email,row in users.items(): grouped.setdefault(mailbox_key(email),[]).append(row)
        allowed={mailbox_key(OWNER_EMAIL)}
        allowed.update(email for email,rows in grouped.items() if len(rows)==1 and rows[0].get('enabled') and 'yas-karting' in rows[0].get('projects',[]))
        for code, room in list(self.rooms.items()):
            if now-room['touched'] > 900 or now-room['created'] > 7200:
                del self.rooms[code]; continue
            for ident, player in list(room['players'].items()):
                if mailbox_key(player['email']) not in allowed or now-player['seen'] > 60: del room['players'][ident]
            if not room['players']: del self.rooms[code]

    def pose(self, slot=0):
        route = self.geometry(); p, q = route['points'][:2]
        angle=math.atan2(q['y']-p['y'],q['x']-p['x'])
        back=(slot//2)*12;side=(slot%2-.5)*8
        return dict(x=p['x']-back*math.cos(angle)-side*math.sin(angle), y=p['y']-back*math.sin(angle)+side*math.cos(angle), angle=angle, speed=0, steer=0)

    def restart(self, room):
        room.update(phase='waiting', startAt=None, epoch=room['epoch']+1)
        for slot,player in enumerate(room['players'].values()):
            player.update(slot=slot,pose=self.pose(slot), gate=1, laps=0, finished=None, lastLap=None, lapStart=None, distance=0, poseAt=self.clock(),rankedLap=True,rankedRace=True,bestLap=None)

    def snapshot(self, room, player, identity):
        now = self.clock()
        if room['phase']=='countdown' and now >= room['startAt']: room['phase']='racing'
        can_control = identity['owner'] or identity['email']==room['host']
        players=[]
        for p in room['players'].values():
            players.append(dict(id=p['id'], name=p['name'], color=p['color'], pose=p['pose'], laps=p['laps'], gate=p['gate'], finished=p['finished'], lastLap=p['lastLap'], bestLap=p.get('bestLap'), connected=now-p['seen']<4, isHost=p['email']==room['host']))
        players.sort(key=lambda p:(p['finished'] is None, p['finished'] or 0, -p['laps'], -p['gate']))
        return dict(code=room['code'], epoch=room['epoch'], phase=room['phase'], startAt=room['startAt'], serverTime=now, options=room['options'], raceLaps=room['raceLaps'], players=players, you=player['id'], canControl=can_control, owner=identity['owner'], rules=KART_RULES,trial=room.get('trial',False),leaderboardWarning=self.leaderboard_warning)

    def request(self, value, identity, access):
        if not isinstance(value,dict): raise ValueError('Invalid room request.')
        try: client = str(uuid.UUID(value.get('clientId','')))
        except (TypeError,ValueError,AttributeError): raise ValueError('Reload this tab before joining.')
        action=value.get('action');email=identity['email'];now=self.clock()
        with self.lock:
            self.sweep(access)
            self.flush_results()
            if action in ('create','join'):
                if value.get('rules')!=KART_RULES: raise ValueError('Read and accept the Paddock Rules first.')
                name=value.get('name')
                if not isinstance(name,str) or not 1<=len(name.strip())<=24 or any(ord(c)<32 for c in name): raise ValueError('Use a driver name of 1–24 characters.')
            if action=='create':
                # Retried create requests recover the same room, including after a lost response.
                existing=next((r for r in self.rooms.values() if r['host']==email and any(p['client']==client and p['email']==email for p in r['players'].values())),None)
                if existing: room=existing
                else:
                    if len(self.rooms)>=32 or sum(r['host']==email for r in self.rooms.values())>=2: raise ValueError('Room limit reached. Leave an old room or try later.')
                    options=value.get('options',{})
                    if not isinstance(options,dict) or set(options)!={'power','maxSpeed','brakes','grip','wet','tyres'}: raise ValueError('Invalid kart settings.')
                    for key,lo,hi in [('power',.6,1.5),('maxSpeed',35,90),('brakes',.6,1.5),('grip',.6,1.4)]:
                        if type(options.get(key)) not in (int,float) or not math.isfinite(options[key]) or not lo<=options[key]<=hi: raise ValueError('Invalid kart performance.')
                    if type(options['wet']) is not bool or options['tyres'] not in ('warm','cold'): raise ValueError('Invalid conditions.')
                    laps=value.get('laps',3)
                    if type(laps) is not int or laps not in (1,3,5): raise ValueError('Choose 1, 3 or 5 laps.')
                    code=''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))
                    while code in self.rooms: code=''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))
                    trial=value.get('trial',False)
                    if type(trial) is not bool:raise ValueError('Invalid time-trial mode.')
                    room=dict(code=code,host=email,created=now,touched=now,players={},blocked=set(),options=dict(options),raceLaps=laps,phase='waiting',startAt=None,epoch=0,trial=trial)
                    self.rooms[code]=room
            else:
                code=value.get('code')
                if not isinstance(code,str) or code not in self.rooms: raise LookupError('Room expired or not found. Ask for a new room code.')
                room=self.rooms[code]
            if email in room['blocked']: raise PermissionError('You were removed from this room.')
            player=next((p for p in room['players'].values() if p['email']==email and p['client']==client),None)
            if action in ('create','join') and player is None:
                if room.get('trial') and room['players']:raise ValueError('This is a solo time trial. Create a multiplayer room to race together.')
                if room['phase']!='waiting': raise ValueError('This race has started. Join after the host selects Rematch.')
                if len(room['players'])>=8: raise ValueError('This room has eight drivers already.')
                if any(p['email']==email for p in room['players'].values()): raise ValueError('You already joined in another tab. Leave that tab’s room first.')
                ident=uuid.uuid4().hex
                used={p['color'] for p in room['players'].values()}
                slot=next(i for i in range(8) if i not in {p['slot'] for p in room['players'].values()})
                player=dict(id=ident,email=email,client=client,name=name.strip(),color=next(c for c in KART_COLORS if c not in used),slot=slot,seen=now,poseAt=now,pose=self.pose(slot),gate=1,laps=0,finished=None,lastLap=None,lapStart=None,distance=0)
                room['players'][ident]=player
            if player is None: raise PermissionError('Join this room before using it.')
            can_control=identity['owner'] or email==room['host']
            if action in ('start','rematch','weather','kick','end') and not can_control: raise PermissionError('Only the host or owner can use race controls.')
            if action=='start':
                if room['phase']!='waiting': raise ValueError('Race already started. Choose Rematch first.')
                if len(room['players'])<(1 if room.get('trial') else 2): raise ValueError('Wait for at least one friend before starting.')
                self.restart(room);room.update(phase='countdown',startAt=now+5)
                for p in room['players'].values(): p['lapStart']=now+5
            elif action=='rematch': self.restart(room)
            elif action=='weather':
                if type(value.get('wet')) is not bool: raise ValueError('Choose dry or wet.')
                room['options']['wet']=value['wet'];self.restart(room)
            elif action=='kick':
                target=room['players'].get(value.get('target'))
                if not target or target['id']==player['id'] or mailbox_key(target['email'])==mailbox_key(OWNER_EMAIL): raise ValueError('That driver cannot be removed.')
                if target['email']==room['host'] and not identity['owner']: raise PermissionError('Only the owner can remove the host.')
                room['blocked'].add(target['email']);del room['players'][target['id']]
            elif action in ('leave','end'):
                if action=='end' or email==room['host']: del self.rooms[room['code']]
                else: del room['players'][player['id']]
                return {'left':True}
            elif action=='recover':
                if value.get('epoch')==room['epoch'] and room['startAt'] and now>=room['startAt'] and player['finished'] is None:
                    gate=self.geometry()['gates'][max(0,player['gate']-1)]
                    p=self.geometry()['points'][(gate['index']+2)%len(self.geometry()['points'])]
                    player['pose']=dict(x=gate['x'],y=gate['y'],angle=math.atan2(p['y']-gate['y'],p['x']-gate['x']),speed=0,steer=0)
                    player['poseAt']=now
                    player['rankedLap']=False;player['rankedRace']=False
            elif action=='sync' and value.get('epoch')==room['epoch'] and room['startAt'] and now>=room['startAt'] and player['finished'] is None:
                pose=value.get('pose')
                if not isinstance(pose,dict) or set(pose)!={'x','y','angle','speed','steer'} or any(type(v) not in (int,float) or not math.isfinite(v) for v in pose.values()): raise ValueError('Invalid kart position.')
                ppm=self.geometry()['lengthPixels']/774
                if not (0<=pose['x']<=780 and 0<=pose['y']<=561 and abs(pose['angle'])<=1e6 and abs(pose['steer'])<=1.01 and abs(pose['speed'])<=26*ppm): raise ValueError('Invalid kart position.')
                elapsed=max(0,now-player['poseAt']);distance=math.hypot(pose['x']-player['pose']['x'],pose['y']-player['pose']['y'])
                # Reject teleports. A reconnect resumes at the last accepted pose.
                if distance<=4*ppm+min(elapsed,2)*28*ppm:
                    player['pose']=dict(pose);player['distance']+=distance
                    gates=self.geometry()['gates'];target=gates[player['gate']]
                    if math.hypot(pose['x']-target['x'],pose['y']-target['y'])<18:
                        player['gate']+=1
                        if player['gate']==len(gates):
                            if now-player['lapStart']>=774/(room['options']['maxSpeed']/3.6)*.65:
                                player['laps']+=1;player['lastLap']=now-player['lapStart'];player['lapStart']=now
                                if player['rankedLap']:
                                    player['bestLap']=min(player['bestLap'] or float('inf'),player['lastLap']);self.queue_result(room,player,'lap',player['lastLap'])
                                if player['laps']>=room['raceLaps']:
                                    player['finished']=now-room['startAt']
                                    if player['rankedRace']:self.queue_result(room,player,'race',player['finished'])
                                player['rankedLap']=True
                            player['gate']=1
                    player['poseAt']=now
                else:
                    player['rankedLap']=False;player['rankedRace']=False
                    player['seen']=now
                    return {**self.snapshot(room,player,identity),'resync':True}
            elif action not in ('create','join','sync','start','rematch','weather','kick','end','leave','recover'): raise ValueError('Unknown room action.')
            player['seen']=now;room['touched']=now
            self.flush_results()
            if room['players'] and all(p['finished'] is not None for p in room['players'].values()):room['phase']='finished'
            return self.snapshot(room,player,identity)

def karting_friends(access, value=None, revision=None, actor=None):
    """Yas-only shortcut over the SAME central access store, never a second account DB."""
    with access.lock:
        if value is not None:
            if not isinstance(value,dict) or set(value)!={'email','action'} or value['action'] not in ('add','remove'): raise ValueError('Choose add or remove and an email.')
            email=mailbox_key(value['email']);doc=access.document()
            if email==mailbox_key(OWNER_EMAIL): raise ValueError('Owner access cannot be changed here.')
            if revision!=access.revision(doc): raise FileExistsError('Access changed. Refresh the list before retrying.')
            matches=[row for address,row in doc['users'].items() if mailbox_key(address)==email]
            if len(matches)>1: raise ValueError('Resolve duplicate email entries in People & permissions first.')
            row=matches[0] if matches else {'projects':[],'enabled':False}
            other=[p for p in row['projects'] if p!='yas-karting']
            if value['action']=='add' and other and not row['enabled']: raise ValueError('This person has revoked access to other projects. Review their permissions in the main form before restoring them.')
            projects=other+(['yas-karting'] if value['action']=='add' or not other else [])
            enabled=True if value['action']=='add' else bool(other) and row['enabled']
            access.update({'email':email,'projects':projects,'enabled':enabled},revision,actor)
        listing,etag=access.users()
        return {'users':[{'email':u['email'],'enabled':u['enabled'],'otherAccess':any(p!='yas-karting' for p in u['projects'])} for u in listing['users'] if 'yas-karting' in u['projects']]},etag

def karting_multiplayer_request(handler, rooms, access):
    path=handler.path.split('?')[0]
    if path not in ('/yas-karting/api/session','/yas-karting/api/room','/yas-karting/api/friends','/yas-karting/api/leaderboard'): return handler.send_json({'error':'Unknown karting endpoint.'},404)
    if path.endswith('/friends') and not handler.identity['owner']: return handler.send_json({'error':'Only the owner can manage access.'},403)
    try:
        if handler.command=='GET':
            if path.endswith('/leaderboard'):return handler.send_json(rooms.leaderboard.listing(handler.identity['email']) if rooms.leaderboard else {'groups':[]})
            if path.endswith('/session'): return handler.send_json({'owner':handler.identity['owner'],'rules':KART_RULES})
            if path.endswith('/friends'):
                result,etag=karting_friends(access);return handler.send_json(result,etag=etag)
            return handler.send_json({'error':'Use a room action.'},405)
        if handler.command!='POST' or path.endswith(('/session','/leaderboard')): return handler.send_json({'error':'Method not allowed.'},405)
        if not handler.origin_ok(): return handler.reject_origin()
        if handler.headers.get('Content-Type','').split(';')[0]!='application/json': return handler.send_json({'error':'Send JSON.'},415)
        n=int(handler.headers.get('Content-Length','0'))
        if not 1<=n<=4096 or handler.headers.get('Transfer-Encoding'): return handler.send_json({'error':'Room request is too large.'},413)
        value=json.loads(handler.rfile.read(n))
        if path.endswith('/friends'):
            result,etag=karting_friends(access,value,handler.headers.get('If-Match'),handler.identity['email']);return handler.send_json(result,etag=etag)
        return handler.send_json(rooms.request(value,handler.identity,access))
    except FileExistsError as e: return handler.send_json({'error':str(e)},409)
    except LookupError as e: return handler.send_json({'error':str(e)},404)
    except PermissionError as e: return handler.send_json({'error':str(e)},403)
    except (ValueError,TypeError,UnicodeError) as e: return handler.send_json({'error':str(e) or 'Check the room details.'},400)
    except OSError: return handler.send_json({'error':'NAS service unavailable. Try again shortly.'},503)
