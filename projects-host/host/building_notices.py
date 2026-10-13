"""Owner-only C11 notices. Uses the existing HA OAuth connection, never new credentials."""
import copy, datetime as dt, hashlib, json, re, threading, urllib.parse, urllib.request, urllib.error
from home_assistant import HAError, HA_URL, ha_json, ha_atomic, ha_read

CALENDAR_NAME = 'Building Notices'
CALENDAR_ID = 'calendar.building_notices'
REMINDER_ID = 'mungoon_building_notices_30m'
RECIPIENTS = ('notify.s25u_leras', 'notify.tabs9_leras', 'notify.s24p_kotie')
DUBAI = dt.timezone(dt.timedelta(hours=4))

def notice_hash(value):
    return hashlib.sha256(ha_json(value)).hexdigest()

def notice_validate(value):
    if not isinstance(value, dict) or set(value)-{'event_id','source_id','sender','building','category','summary','description','start','end','cancelled','dry_run'}:
        raise ValueError('Invalid building notice fields.')
    result = copy.deepcopy(value)
    if result.get('sender') != 'support@avisonyoung.ae' or result.get('building') != 'C11':
        raise ValueError('Only verified C11 notices from Avison Young are supported.')
    if result.get('category') not in ('alarm_test','maintenance','service_interruption'):
        raise ValueError('Unsupported notice category.')
    if not re.fullmatch(r'[a-z0-9_-]{1,100}', str(result.get('event_id',''))) or not re.fullmatch(r'[a-f0-9]{16}',str(result.get('source_id',''))):
        raise ValueError('A stable event ID and verified Gmail message ID are required.')
    for key,limit in (('summary',200),('description',3000)):
        text=result.get(key,'')
        if not isinstance(text,str) or not text.strip() or len(text)>limit or any(ord(c)<32 and c not in '\n\r\t' for c in text):
            raise ValueError('Invalid notice '+key+'.')
        result[key]=text.strip()
    for key in ('cancelled','dry_run'):
        if key in result and not isinstance(result[key],bool): raise ValueError('Invalid '+key+'.')
    for key in ('start','end'):
        try:
            stamp=dt.datetime.fromisoformat(result[key].replace('Z','+00:00'))
            if stamp.tzinfo is None: raise ValueError()
            result[key]=stamp.astimezone(DUBAI).isoformat()
        except (ValueError,TypeError,KeyError): raise ValueError('Use timezone-aware start and end timestamps.') from None
    start,end=(dt.datetime.fromisoformat(result[k]) for k in ('start','end'))
    if not dt.timedelta(minutes=1)<=end-start<=dt.timedelta(days=7): raise ValueError('Invalid notice duration.')
    if start.year<2020 or start>dt.datetime.now(DUBAI)+dt.timedelta(days=732): raise ValueError('Invalid notice date.')
    result.setdefault('cancelled',False)
    result.pop('dry_run',None)
    return result

def calendar_event(notice):
    return {'summary':('[Cancelled] ' if notice['cancelled'] else '')+'C11 — '+notice['summary'],
            'start':notice['start'],'end':notice['end'],'location':'C11',
            'description':notice['description']+'\n\nBuildingNotice-ID: '+notice['event_id']+'\nSource message: '+notice['source_id']+'\nSource: https://mail.google.com/mail/u/?authuser=lamontleras%40gmail.com#all/'+notice['source_id']}

def reminder_config():
    return {'id':REMINDER_ID,'alias':'Building Notices — 30-minute reminders','mode':'queued','max':20,
            'triggers':[{'trigger':'calendar','entity_id':CALENDAR_ID,'event':'start','offset':'-00:30:00'}],
            'conditions':[{'condition':'template','value_template':"{{ not trigger.calendar_event.summary.startswith('[Cancelled]') and 'BuildingNotice-ID:' in (trigger.calendar_event.description or '') and as_timestamp(trigger.calendar_event.start) > as_timestamp(now()) }}"}],
            'actions':[{'action':'notify.send_message','continue_on_error':True,'target':{'entity_id':recipient},
                        'data':{'title':'Building notice — starts in 30 minutes','message':"{{ trigger.calendar_event.summary }}\n{{ as_datetime(trigger.calendar_event.start).strftime('%d %b %Y %H:%M') }}–{{ as_datetime(trigger.calendar_event.end).strftime('%H:%M') }} (Dubai)\n{{ trigger.calendar_event.description }}"}} for recipient in RECIPIENTS]}

class BuildingNotices:
    def __init__(self,data,ha):
        self.path=data/'building-notices.json';self.ha=ha;self.lock=threading.RLock()
    def load(self): return ha_read(self.path,{'version':1,'events':{}})
    def rest(self,path,method='GET',body=None):
        req=urllib.request.Request(HA_URL+path,data=ha_json(body) if body is not None else None,
             headers={'Authorization':'Bearer '+self.ha.token(),'Content-Type':'application/json'},method=method)
        try:
            with urllib.request.urlopen(req,timeout=25) as response: return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code==404 and method=='GET': return None
            raise HAError('HA rejected the Building Notices '+method+' request ('+str(exc.code)+').') from None
        except (OSError,ValueError): raise HAError('HA did not confirm this request; retry safely.') from None
    def metadata(self):
        with self.ha.websocket(self.ha.token()) as ws:
            states=ws.call('get_states');services=ws.call('get_services');registry=ws.call('config/entity_registry/list');config=ws.call('get_config')
        active={e['entity_id']:e for e in states}
        disabled={e['entity_id'] for e in registry if e.get('disabled_by')}
        targets=[{'entity_id':eid,'name':e.get('attributes',{}).get('friendly_name',eid)} for eid,e in active.items() if eid.startswith('notify.') and eid not in disabled]
        calendars=[{'entity_id':eid,'name':e.get('attributes',{}).get('friendly_name',eid)} for eid,e in active.items() if eid.startswith('calendar.')]
        return {'timezone':config.get('time_zone'),'targets':targets,'calendars':calendars,
                'notify_services':sorted(services.get('notify',{})),'calendar_services':sorted(services.get('calendar',{})),
                'reminder_state':active.get('automation.building_notices_30_minute_reminders',{}).get('state'),
                'events':self.load()['events'],'cloud_intake':'unconfigured; existing owner authentication required'}
    def prepare_google(self,value):
        if value!={}: raise ValueError('Google setup preparation takes no credentials or configuration.')
        # Initiate only the built-in setup dialog. Never submit credentials or follow the authorization URL.
        with self.lock:
            doc=self.load();flow_id=doc.get('google_setup_flow')
            flow=self.rest('/api/config/config_entries/flow/'+flow_id) if flow_id else None
            if flow is None:
                flow=self.rest('/api/config/config_entries/flow','POST',{'handler':'google','show_advanced_options':False})
            if flow.get('flow_id') and flow.get('type') not in ('abort','create_entry'):
                doc['google_setup_flow']=flow['flow_id'];ha_atomic(self.path,doc)
        fields=[{'name':s.get('name'),'type':s.get('type'),'required':s.get('required',False)} for s in flow.get('data_schema',[])]
        result={'flow_id':flow.get('flow_id'),'type':flow.get('type'),'step_id':flow.get('step_id'),'fields':fields,
                'credentials_submitted':False,'oauth_granted':False,'calendar_changes':False,'notification_sent':False}
        if isinstance(flow.get('reason'),str) and re.fullmatch(r'[a-z0-9_]{1,100}',flow['reason']):result['reason']=flow['reason']
        if flow.get('url'):
            query=urllib.parse.parse_qs(urllib.parse.urlsplit(flow['url']).query)
            result['authorization_host']=urllib.parse.urlsplit(flow['url']).netloc
            result['requested_scopes']=query.get('scope',[''])[0].split()
            result['offline_access']=query.get('access_type')==['offline']
        return result
    def setup(self):
        with self.lock:
            meta=self.metadata()
            ids={t['entity_id'] for t in meta['targets']}
            if ids!=set(RECIPIENTS): raise HAError('Notification devices changed. Review live targets before setup.')
            if meta['timezone']!='Asia/Dubai' or 'send_message' not in meta['notify_services']: raise HAError('Dubai timezone and notify.send_message are required.')
            if not any(c['entity_id']==CALENDAR_ID for c in meta['calendars']):
                if any(c['name']==CALENDAR_NAME for c in meta['calendars']): raise HAError('A Building Notices calendar already uses another entity ID; review before creating.')
                flow=self.rest('/api/config/config_entries/flow','POST',{'handler':'local_calendar','show_advanced_options':False})
                if flow.get('type')!='form' or flow.get('step_id')!='user': raise HAError('Unexpected Local Calendar setup; no credential changes made.')
                flow=self.rest('/api/config/config_entries/flow/'+flow['flow_id'],'POST',{'calendar_name':CALENDAR_NAME,'import':'create_empty'})
                if flow.get('type')!='create_entry': raise HAError('HA did not confirm Building Notices creation.')
            meta=self.metadata()
            if not any(c['entity_id']==CALENDAR_ID for c in meta['calendars']): raise HAError('Calendar is starting; retry setup to verify it.')
            config=reminder_config()
            path='/api/config/automation/config/'+REMINDER_ID
            existing=self.rest(path)
            doc=self.load()
            if existing is not None and not doc.get('reminder_config'):
                if existing!=config: raise HAError('An existing reminder uses this ID; preserve it and review.')
            if existing is not None and doc.get('reminder_config'):
                from ha_automations import automation_config_hash
                if automation_config_hash(existing)!=automation_config_hash(doc['reminder_config']): raise HAError('Reminder changed in HA; no changes overwritten.')
            if existing is None or automation_config_hash(existing)!=automation_config_hash(config):
                self.rest(path,'POST',config)
            confirmed=self.rest(path)
            from ha_automations import automation_config_hash
            if confirmed is None or automation_config_hash(confirmed)!=automation_config_hash(config): raise HAError('Reminder configuration was not confirmed.')
            doc['reminder_config']=confirmed;ha_atomic(self.path,doc)
            return {'calendar':CALENDAR_ID,'targets':meta['targets'],'offset':'-00:30:00','configured':True,'notification_sent':False}
    def events(self,start,end):
        query=urllib.parse.urlencode({'start':start,'end':end})
        return self.rest('/api/calendars/'+CALENDAR_ID+'?'+query) or []
    def find(self,notice,record):
        windows={(notice['start'],notice['end'])}
        for n in (record.get('notice'),record.get('desired')):
            if n: windows.add((n['start'],n['end']))
        matches={}
        marker='BuildingNotice-ID: '+notice['event_id']
        for start,end in windows:
            for event in self.events(start,end):
                if marker in (event.get('description') or '').splitlines():
                    matches[event['uid']]=event
        if len(matches)>1: raise HAError('Duplicate notice markers found; preserve and review them.')
        return next(iter(matches.values()),None)
    def comparable(self,event):
        result={k:event.get(k,'') for k in ('summary','description','location')}
        for k in ('start','end'):
            val=event[k];val=val.get('dateTime') if isinstance(val,dict) else val
            result[k]=dt.datetime.fromisoformat(val).astimezone(DUBAI).isoformat()
        return result
    def upsert(self,value):
        notice=notice_validate(value);desired=calendar_event(notice)
        with self.lock:
            doc=self.load();record=doc['events'].get(notice['event_id'],{})
            remote=self.find(notice,record)
            if remote and record.get('confirmed') and self.comparable(remote) not in (record['confirmed'],desired):
                raise HAError('This notice was changed manually in HA; no changes overwritten.')
            operation='unchanged' if remote and self.comparable(remote)==desired else 'amend' if remote else 'create'
            if value.get('dry_run'):
                return {'operation':operation,'event':desired,'targets':list(RECIPIENTS),'reminder_in_past':dt.datetime.fromisoformat(notice['start'])-dt.timedelta(minutes=30)<=dt.datetime.now(DUBAI),'notification_sent':False}
            if record.get('confirmed') and not remote: raise HAError('Saved notice is missing from HA; review before recreating.')
            if notice['cancelled'] and not remote: raise ValueError('Cannot cancel a notice that has not been imported.')
            record['desired']=notice;doc['events'][notice['event_id']]=record;ha_atomic(self.path,doc)
            if operation!='unchanged':
                wire={('dtstart' if k=='start' else 'dtend' if k=='end' else k):v for k,v in desired.items()}
                with self.ha.websocket(self.ha.token()) as ws:
                    if remote: ws.call('calendar/event/update',entity_id=CALENDAR_ID,uid=remote['uid'],event=wire)
                    else: ws.call('calendar/event/create',entity_id=CALENDAR_ID,event=wire)
            remote=self.find(notice,record)
            if not remote or self.comparable(remote)!=desired: raise HAError('Calendar did not confirm the event; retry with the same stable event ID.')
            record.update(notice=notice,confirmed=desired,uid=remote['uid'],status='cancelled' if notice['cancelled'] else 'active')
            record.setdefault('source_ids',[])
            if notice['source_id'] not in record['source_ids']: record['source_ids'].append(notice['source_id'])
            record.pop('desired',None);ha_atomic(self.path,doc)
            return {'operation':operation,'uid':remote['uid'],'calendar':CALENDAR_ID,'event':desired,'notification_sent':False}
    def notify_current(self,value):
        if not isinstance(value,dict) or set(value)-{'event_id','dry_run'} or not isinstance(value.get('event_id'),str) or ('dry_run' in value and not isinstance(value['dry_run'],bool)):
            raise ValueError('Provide a saved event ID and optional dry_run flag.')
        with self.lock:
            doc=self.load();record=doc['events'].get(value['event_id'])
            if not record or not record.get('confirmed') or record.get('status')!='active': raise ValueError('An active verified imported notice is required.')
            notice=record['notice'];now=dt.datetime.now(DUBAI)
            start,end=(dt.datetime.fromisoformat(notice[k]) for k in ('start','end'))
            if not start<=now<end: raise ValueError('Current notices are only sent while the event is in progress.')
            remote=self.find(notice,record)
            if not remote or self.comparable(remote)!=record['confirmed']: raise HAError('Notice changed in HA; review it before sending.')
            title='Building notice — already started'
            message='C11 — '+notice['summary']+'\n'+start.strftime('%d %b %Y %H:%M')+'–'+end.strftime('%H:%M')+' (Dubai)\n'+notice['description']
            if value.get('dry_run'): return {'dry_run':True,'title':title,'message':message,'targets':list(RECIPIENTS),'notification_sent':False}
            meta=self.metadata()
            if not set(RECIPIENTS).issubset({t['entity_id'] for t in meta['targets']}): raise HAError('A notification device is missing; no current notice sent.')
            status=record.setdefault('current_notifications',{})
            with self.ha.websocket(self.ha.token()) as ws:
                for recipient in RECIPIENTS:
                    # Mark before sending. An interrupted or ambiguous send is never retried automatically.
                    if recipient in status: continue
                    status[recipient]='pending';ha_atomic(self.path,doc)
                    try:
                        ws.call('call_service',domain='notify',service='send_message',target={'entity_id':recipient},service_data={'title':title,'message':message})
                        status[recipient]='accepted'
                    except HAError:
                        status[recipient]='unconfirmed'
                    ha_atomic(self.path,doc)
            return {'event_id':value['event_id'],'recipients':status,'delivery_on_device':'not verified','duplicates_automatically_suppressed':True}

def building_notices_request(handler,notices):
    # Called only after gateway authorization; owner policy also applies to all /api/admin routes.
    if not handler.identity.get('owner'): return handler.send_json({'error':'Owner only.'},403)
    try:
        if handler.command=='GET': return handler.send_json(notices.metadata())
        if handler.command!='POST': return handler.send_error(405)
        if not handler.origin_ok(): return handler.reject_origin()
        n=int(handler.headers.get('Content-Length','0'))
        if not 0<n<=12000 or handler.headers.get('Transfer-Encoding'): raise ValueError('Invalid request size.')
        if handler.headers.get('Content-Type','').split(';')[0]!='application/json': return handler.send_error(415)
        value=json.loads(handler.rfile.read(n))
        if handler.path.endswith('/setup'):
            if value!={}: raise ValueError('Setup takes no configuration fields.')
            return handler.send_json(notices.setup())
        if handler.path.endswith('/event'): return handler.send_json(notices.upsert(value))
        if handler.path.endswith('/notify-current'): return handler.send_json(notices.notify_current(value))
        if handler.path.endswith('/google-prepare'): return handler.send_json(notices.prepare_google(value))
        return handler.send_error(404)
    except (ValueError,TypeError) as exc: return handler.send_json({'error':str(exc)},400)
    except HAError as exc: return handler.send_json({'error':str(exc)},409)
    except Exception: return handler.send_json({'error':'Building Notices request was not confirmed; retry safely.'},502)
