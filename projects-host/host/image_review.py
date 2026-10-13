"""Owner-only image review. Derived previews and SQLite decisions stay in /srv/data."""
import csv
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REVIEW_BANDS = ('Borderline', 'Likely memes', 'Likely non-memes')
CATALOG_BANDS = (*REVIEW_BANDS,'Photos','Memes','Screenshots','Documents','Graphics','Technical review')
REVIEW_HASH = re.compile(r'^[a-f0-9]{64}$')
REVIEW_OP = re.compile(r'^[a-f0-9-]{32,40}$')
ADULT_MODEL = 'Marqo/nsfw-image-detection-384'
ADULT_MODEL_REVISION = '0c26ec22111b83f106d72a55f611ec35962bcb65'

class ReviewConflict(Exception):
    pass

class ImageReview:
    def __init__(self, data):
        self.root = Path(data) / 'image-review'
        self.media = self.root / 'previews'
        self.lock = threading.RLock()
        self.initialized = False

    def connect(self):
        with self.lock:
            self.media.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.root / 'decisions.sqlite3', timeout=30)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            if not self.initialized:
                db.executescript('''
                  CREATE TABLE IF NOT EXISTS items (
                    sha256 TEXT PRIMARY KEY, band TEXT NOT NULL, score REAL NOT NULL,
                    subtype TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                    media_sha TEXT NOT NULL, media_size INTEGER NOT NULL,
                    ready INTEGER NOT NULL DEFAULT 0, label TEXT,
                    revision INTEGER NOT NULL DEFAULT 0, updated TEXT);
                  CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL, sha256 TEXT NOT NULL,
                    label TEXT, previous_label TEXT, revision INTEGER NOT NULL,
                    kind TEXT NOT NULL, undo_of INTEGER, at TEXT NOT NULL, actor TEXT NOT NULL);
                  CREATE INDEX IF NOT EXISTS item_queue ON items(band,ready,label);
                  CREATE TABLE IF NOT EXISTS learning (
                    id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL);
                  CREATE TABLE IF NOT EXISTS people (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL UNIQUE);
                  CREATE TABLE IF NOT EXISTS annotations (
                    sha256 TEXT NOT NULL, channel TEXT NOT NULL, value TEXT NOT NULL,
                    revision INTEGER NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(sha256,channel));
                  CREATE TABLE IF NOT EXISTS annotation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL, sha256 TEXT NOT NULL, channel TEXT NOT NULL,
                    value TEXT NOT NULL, previous_value TEXT NOT NULL, revision INTEGER NOT NULL,
                    kind TEXT NOT NULL, undo_of INTEGER, at TEXT NOT NULL, actor TEXT NOT NULL);
                  CREATE TABLE IF NOT EXISTS image_people (
                    sha256 TEXT NOT NULL, person TEXT NOT NULL, PRIMARY KEY(sha256,person));
                  CREATE INDEX IF NOT EXISTS people_images ON image_people(person,sha256);
                  CREATE TABLE IF NOT EXISTS adult_predictions (
                    sha256 TEXT PRIMARY KEY, media_sha TEXT NOT NULL, score REAL NOT NULL,
                    model TEXT NOT NULL, model_revision TEXT NOT NULL, updated TEXT NOT NULL);
                  CREATE INDEX IF NOT EXISTS adult_prediction_score ON adult_predictions(score,sha256);
                ''')
                self.initialized = True
            return db

    def import_items(self, items):
        if not isinstance(items, list) or not 1 <= len(items) <= 2500:
            raise ValueError('Invalid catalog batch.')
        rows = []
        required = {'sha256', 'band', 'score', 'subtype', 'width', 'height', 'media_sha', 'media_size'}
        for row in items:
            if not isinstance(row, dict) or set(row) != required:
                raise ValueError('Invalid catalog fields.')
            if not REVIEW_HASH.fullmatch(str(row['sha256'])) or not REVIEW_HASH.fullmatch(str(row['media_sha'])):
                raise ValueError('Invalid image identifier.')
            if row['band'] not in CATALOG_BANDS or row['subtype'] not in ('Photo', 'Screenshot', 'Document', 'Graphic'):
                raise ValueError('Invalid category.')
            if type(row['score']) not in (float, int) or not math.isfinite(row['score']) or abs(row['score']) > 100:
                raise ValueError('Invalid ranking margin.')
            if any(type(row[k]) is not int or not 1 <= row[k] <= 100000 for k in ('width', 'height')):
                raise ValueError('Invalid image dimensions.')
            if type(row['media_size']) is not int or not 4 <= row['media_size'] <= 1800000:
                raise ValueError('Invalid preview size.')
            rows.append(tuple(row[k] for k in ('sha256','band','score','subtype','width','height','media_sha','media_size')))
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            for row in rows:
                old = db.execute('SELECT sha256,band,score,subtype,width,height,media_sha,media_size FROM items WHERE sha256=?', (row[0],)).fetchone()
                if old and tuple(old) != row:
                    raise ReviewConflict('This image already has different catalog data. Nothing replaced.')
                if not old:
                    db.execute('INSERT INTO items(sha256,band,score,subtype,width,height,media_sha,media_size) VALUES(?,?,?,?,?,?,?,?)', row)
            db.commit()
            return {'imported': len(rows)}
        finally:
            db.close()

    def import_media(self, ident, body):
        if not REVIEW_HASH.fullmatch(ident): raise ValueError('Invalid image identifier.')
        if not body.startswith(b'\xff\xd8') or not body.endswith(b'\xff\xd9'):
            raise ValueError('Expected a JPEG preview.')
        db = self.connect()
        try:
            with self.lock:
                row = db.execute('SELECT media_sha,media_size FROM items WHERE sha256=?', (ident,)).fetchone()
                if not row: raise ValueError('Import the catalog first.')
                if len(body) != row['media_size'] or hashlib.sha256(body).hexdigest() != row['media_sha']:
                    raise ValueError('Preview hash mismatch.')
                target = self.media / (ident + '.jpg')
                if target.exists():
                    if hashlib.sha256(target.read_bytes()).hexdigest() != row['media_sha']:
                        raise ReviewConflict('Existing preview differs. Nothing replaced.')
                else:
                    temp = target.with_suffix('.tmp')
                    with temp.open('wb') as out:
                        out.write(body); out.flush(); os.fsync(out.fileno())
                    os.replace(temp, target)
                db.execute('UPDATE items SET ready=1 WHERE sha256=?', (ident,)); db.commit()
            return {'ready': True, 'sha256': ident}
        finally: db.close()

    @staticmethod
    def public(row):
        return {k: row[k] for k in ('sha256','band','score','subtype','width','height','label','revision','updated')}

    @staticmethod
    def annotation(db, ident, channel):
        row=db.execute('SELECT * FROM annotations WHERE sha256=? AND channel=?',(ident,channel)).fetchone()
        default={'state':None,'people':[],'other':False} if channel=='people' else {'state':None}
        return {**(json.loads(row['value']) if row else default),'revision':row['revision'] if row else 0}

    @staticmethod
    def adult_prediction(db, ident):
        row=db.execute('SELECT score,model,model_revision,updated FROM adult_predictions WHERE sha256=?',(ident,)).fetchone()
        if not row:return None
        return {**dict(row),'band':'likely_adult' if row['score']>=.85 else 'likely_safe' if row['score']<=.15 else 'uncertain'}

    @staticmethod
    def adult_prediction_stats(db):
        row=db.execute('SELECT COUNT(*) scored,COALESCE(SUM(score>=0.85),0) likely_adult,COALESCE(SUM(score>0.15 AND score<0.85),0) uncertain,COALESCE(SUM(score<=0.15),0) likely_safe FROM adult_predictions').fetchone()
        return {**dict(row),'model':ADULT_MODEL,'model_revision':ADULT_MODEL_REVISION,
                'ready_total':db.execute('SELECT COUNT(*) FROM items WHERE ready=1').fetchone()[0]}

    def import_adult_predictions(self,value):
        if not isinstance(value,dict) or set(value)!={'model','model_revision','items'} or value['model']!=ADULT_MODEL or value['model_revision']!=ADULT_MODEL_REVISION:raise ValueError('Unknown adult-content model revision.')
        rows=value['items']
        if not isinstance(rows,list) or not 1<=len(rows)<=2500:raise ValueError('Invalid prediction batch.')
        for row in rows:
            if not isinstance(row,dict) or set(row)!={'sha256','media_sha','score'} or not REVIEW_HASH.fullmatch(str(row['sha256'])) or not REVIEW_HASH.fullmatch(str(row['media_sha'])) or type(row['score']) not in (int,float) or not math.isfinite(row['score']) or not 0<=row['score']<=1:raise ValueError('Invalid adult-content prediction.')
        if len({r['sha256'] for r in rows})!=len(rows):raise ValueError('Duplicate image predictions.')
        db=self.connect()
        try:
            db.execute('BEGIN IMMEDIATE');stamp=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
            for row in rows:
                item=db.execute('SELECT ready,media_sha FROM items WHERE sha256=?',(row['sha256'],)).fetchone()
                if not item or not item['ready'] or item['media_sha']!=row['media_sha']:raise ValueError('Prediction does not match an available preview.')
                old=db.execute('SELECT * FROM adult_predictions WHERE sha256=?',(row['sha256'],)).fetchone()
                if old:
                    if old['media_sha']!=row['media_sha'] or old['model']!=value['model'] or old['model_revision']!=value['model_revision'] or not math.isclose(old['score'],row['score'],abs_tol=1e-6):raise ReviewConflict('This image already has a different prediction. Nothing replaced.')
                else:db.execute('INSERT INTO adult_predictions VALUES(?,?,?,?,?,?)',(row['sha256'],row['media_sha'],row['score'],value['model'],value['model_revision'],stamp))
            db.commit();return {'imported':len(rows),'stats':self.adult_prediction_stats(db)}
        finally:db.close()

    def create_person(self, value):
        if not isinstance(value,dict) or set(value)!={'op','name'} or not REVIEW_OP.fullmatch(str(value['op'])) or not isinstance(value['name'],str):raise ValueError('Invalid person tag.')
        name=unicodedata.normalize('NFKC',value['name']).strip()
        if not 1<=len(name)<=64 or any(unicodedata.category(c).startswith('C') for c in name):raise ValueError('Invalid person name.')
        normalized=name.casefold()
        db=self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT * FROM people WHERE id=?',(value['op'],)).fetchone()
            if existing and existing['normalized']!=normalized:raise ReviewConflict('This tag request was used for a different name.')
            existing=existing or db.execute('SELECT * FROM people WHERE normalized=?',(normalized,)).fetchone()
            if existing:return {'id':existing['id'],'name':existing['name']}
            if db.execute('SELECT COUNT(*) FROM people').fetchone()[0]>=300:raise ValueError('Person tag limit reached.')
            db.execute('INSERT INTO people VALUES(?,?,?)',(value['op'],name,normalized));db.commit()
            return {'id':value['op'],'name':name}
        finally:db.close()

    def annotate(self, channel, value, actor, undo=False):
        required={'op','event'} if undo else {'op','sha256','revision','value'}
        if channel not in ('people','adult') or not isinstance(value,dict) or set(value)!=required or not REVIEW_OP.fullmatch(str(value['op'])):raise ValueError('Invalid review.')
        if undo:
            if type(value['event']) is not int:raise ValueError('Invalid event.')
        else:
            if not REVIEW_HASH.fullmatch(str(value['sha256'])) or type(value['revision']) is not int or value['revision']<0:raise ValueError('Invalid image revision.')
            v=value['value']
            if not isinstance(v,dict):raise ValueError('Invalid labels.')
            if channel=='people':
                if set(v)!={'state','people','other'} or v['state'] not in ('tagged','none','skip') or type(v['other']) is not bool or not isinstance(v['people'],list) or len(v['people'])>30 or not all(isinstance(p,str) and REVIEW_OP.fullmatch(p) for p in v['people']) or len(set(v['people']))!=len(v['people']):raise ValueError('Invalid people tags.')
                if (v['state']=='tagged' and not (v['people'] or v['other'])) or (v['state']!='tagged' and (v['people'] or v['other'])):raise ValueError('Select people or choose no people.')
            elif set(v)!={'state'} or v['state'] not in ('adult','not_adult','skip'):raise ValueError('Invalid adult-content label.')
        digest=hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        db=self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            kind='undo' if undo else 'decision'
            old=db.execute('SELECT * FROM annotation_events WHERE op=?',(value['op'],)).fetchone()
            if old:
                if old['request_hash']!=digest or old['kind']!=kind or old['channel']!=channel:raise ReviewConflict('This request was already used for another action.')
                return {'saved':True,'event':old['id'],'sha256':old['sha256'],'revision':old['revision'],'replayed':True}
            source=db.execute('SELECT * FROM annotation_events WHERE id=?',(value['event'],)).fetchone() if undo else None
            if undo and (not source or source['channel']!=channel or source['kind']!='decision'):raise ValueError('Review event not found.')
            ident=source['sha256'] if undo else value['sha256']
            item=db.execute('SELECT ready FROM items WHERE sha256=?',(ident,)).fetchone()
            if not item or not item['ready']:raise ValueError('Image preview is unavailable.')
            before=self.annotation(db,ident,channel);revision=before.pop('revision')
            expected=source['revision'] if undo else value['revision']
            if revision!=expected or undo and db.execute('SELECT 1 FROM annotation_events WHERE undo_of=?',(source['id'],)).fetchone():raise ReviewConflict('This review changed on another device. Refresh first.')
            after=json.loads(source['previous_value']) if undo else value['value']
            if channel=='people':
                for person in after['people']:
                    if not db.execute('SELECT 1 FROM people WHERE id=?',(person,)).fetchone():raise ValueError('Unknown person tag.')
                db.execute('DELETE FROM image_people WHERE sha256=?',(ident,))
                db.executemany('INSERT INTO image_people VALUES(?,?)',[(ident,p) for p in after['people']])
            stamp=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime());revision+=1
            db.execute('INSERT OR REPLACE INTO annotations VALUES(?,?,?,?,?)',(ident,channel,json.dumps(after),revision,stamp))
            event=db.execute('INSERT INTO annotation_events(op,request_hash,sha256,channel,value,previous_value,revision,kind,undo_of,at,actor) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(value['op'],digest,ident,channel,json.dumps(after),json.dumps(before),revision,kind,source['id'] if undo else None,stamp,actor))
            db.commit();return {'saved':True,'event':event.lastrowid,'sha256':ident,'revision':revision}
        finally:db.close()

    @staticmethod
    def learning_state(db):
        counts = dict(db.execute("SELECT label,COUNT(*) FROM items WHERE ready=1 AND band IN (?,?,?) AND label IN ('meme','not_meme') GROUP BY label",REVIEW_BANDS))
        row = db.execute('SELECT document FROM learning WHERE id=1').fetchone()
        model = json.loads(row[0]) if row else None
        active = bool(model and model.get('accepted'))
        if active:
            labels = dict(db.execute('SELECT sha256,label FROM items'))
            active = all(labels.get(h) == label for h,label in model['labels'].items())
        return {'counts':counts,'minimum_total':200,'minimum_each':50,
                'eligible':sum(counts.values()) >= 200 and min(counts.get('meme',0),counts.get('not_meme',0)) >= 50,
                'active':active,'corrected_since_training':bool(model and model.get('accepted') and not active),
                'model':{k:v for k,v in model.items() if k != 'labels'} if model else None,
                'score_schema':'mungoon-refinement-rank-v1'}

    def learning_status(self):
        db = self.connect()
        try:
            db.execute('BEGIN')
            return self.learning_state(db)
        finally: db.close()

    @staticmethod
    def measure(rows, threshold):
        recalls = []
        for label in ('meme','not_meme'):
            group = [r for r in rows if r['label'] == label]
            recalls.append(sum((r['score'] >= threshold) == (label == 'meme') for r in group)/len(group))
        return {'balanced_accuracy':sum(recalls)/2,'meme_recall':recalls[0],'not_meme_recall':recalls[1]}

    @staticmethod
    def fit_threshold(rows):
        # Sweep once, balancing both classes. Ties prefer the original zero cutoff.
        rows = sorted(rows,key=lambda r:r['score'])
        positives = sum(r['label']=='meme' for r in rows)
        negatives = len(rows)-positives
        tp,tn = positives,0
        best = (-1,0,0)
        candidates = [(rows[0]['score'],tp,tn)]
        for i,r in enumerate(rows):
            tp -= r['label']=='meme'; tn += r['label']=='not_meme'
            if i+1 == len(rows) or rows[i+1]['score'] != r['score']:
                threshold = (r['score']+rows[i+1]['score'])/2 if i+1<len(rows) else math.nextafter(r['score'],math.inf)
                candidates.append((threshold,tp,tn))
        for threshold,tp,tn in candidates:
            candidate = ((tp/positives+tn/negatives)/2,-abs(threshold),threshold)
            if candidate[:2] > best[:2]:best=candidate
        return best[2]

    def train(self):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            state = self.learning_state(db)
            if not state['eligible']:
                return {**state,'message':'Keep reviewing: need 200 decisions, including at least 50 of each label.'}
            rows = [dict(r) for r in db.execute("SELECT sha256,score,label FROM items WHERE ready=1 AND band IN (?,?,?) AND label IN ('meme','not_meme')",REVIEW_BANDS)]
            train,test = [],[]
            for label in ('meme','not_meme'):
                group = sorted((r for r in rows if r['label']==label),key=lambda r:hashlib.sha256(('holdout-v1:'+r['sha256']).encode()).hexdigest())
                cut = max(10,len(group)//5)
                test.extend(group[:cut]);train.extend(group[cut:])
            threshold = self.fit_threshold(train)
            baseline = self.measure(test,0.0)
            candidate = self.measure(test,threshold)
            previous = state['model'] if state['active'] else None
            reference = self.measure(test,previous['threshold']) if previous else baseline
            accepted = candidate['balanced_accuracy'] >= max(baseline['balanced_accuracy'],reference['balanced_accuracy'])+.05-1e-12 and all(candidate[k] >= max(baseline[k],reference[k])-1e-12 for k in ('meme_recall','not_meme_recall'))
            report = {'version':time.time_ns(),'trained_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
                      'threshold':threshold,'accepted':accepted,'training_count':len(train),'holdout_count':len(test),
                      'baseline':baseline,'candidate':candidate,'reference':reference,
                      'validation_note':'Preliminary check on your reviewed collection; repeated updates reuse some holdout examples. Check both likely queues too.',
                      'labels':{r['sha256']:r['label'] for r in rows}}
            # A rejected update retains a still-valid earlier filter.
            if accepted or not previous:
                db.execute('INSERT OR REPLACE INTO learning(id,document) VALUES(1,?)',(json.dumps(report),))
            db.commit()
            return {**self.learning_state(db),'attempt':{k:v for k,v in report.items() if k!='labels'},
                    'message':'Improved cutoff activated.' if accepted else 'No verified improvement. Current cutoff retained.'}
        finally: db.close()

    def predict(self, value):
        if not isinstance(value,dict) or set(value) != {'score_schema','items'} or value['score_schema'] != 'mungoon-refinement-rank-v1':
            raise ValueError('Use the same refinement ranking scores.')
        rows = value['items']
        if not isinstance(rows,list) or not 1 <= len(rows) <= 2500:raise ValueError('Invalid batch.')
        for row in rows:
            if not isinstance(row,dict) or set(row) != {'sha256','score'} or not REVIEW_HASH.fullmatch(str(row['sha256'])) or type(row['score']) not in (int,float) or not math.isfinite(row['score']) or abs(row['score'])>100:
                raise ValueError('Invalid score.')
        db = self.connect()
        try:
            db.execute('BEGIN');state=self.learning_state(db)
            threshold=state['model']['threshold'] if state['active'] else 0.0
            results=[]
            for row in rows:
                saved=db.execute('SELECT label FROM items WHERE sha256=?',(row['sha256'],)).fetchone()
                label=saved[0] if saved and saved[0] in ('meme','not_meme') else None
                results.append({**row,'suggestion':label or ('meme' if row['score']>=threshold else 'not_meme'),
                                'source':'your_decision' if label else 'learned_cutoff' if state['active'] else 'original_cutoff',
                                'adjusted_score':row['score']-threshold})
            return {'items':results,'threshold':threshold,'learning_active':state['active'],'scores_are_probabilities':False}
        finally:db.close()

    def queue(self, query):
        band = query.get('band', ['Borderline'])[0]
        state = query.get('state', ['pending'])[0]
        order = query.get('sort', ['uncertain'])[0]
        offset = int(query.get('offset', ['0'])[0])
        mode=query.get('mode',['meme'])[0]
        people_state=query.get('people_state',['all'])[0]
        adult_state=query.get('adult_state',['all'])[0]
        adult_ml=query.get('adult_ml',['all'])[0]
        persons=query.get('person',[])
        if band not in (*CATALOG_BANDS, 'all','priority','meme_candidates') or state not in ('pending','skipped','decided') or not 0 <= offset <= 200000 or mode not in ('meme','people','adult') or people_state not in ('all','unreviewed','tagged','other','none','skip') or adult_state not in ('all','unreviewed','adult','not_adult','skip') or len(persons)>30 or not all(REVIEW_OP.fullmatch(p) for p in persons):
            raise ValueError('Invalid queue filter.')
        if adult_ml not in ('all','flagged','likely_adult','uncertain','likely_safe','unscored'):raise ValueError('Invalid machine filter.')
        sorts = {'uncertain':'ABS(score),sha256', 'meme':'score DESC,sha256', 'nonmeme':'score,sha256',
                 'adult':'COALESCE((SELECT p.score FROM adult_predictions p WHERE p.sha256=items.sha256),-1) DESC,sha256'}
        if order not in sorts: raise ValueError('Invalid sort.')
        where = '1=1' if band == 'all' else 'band IN (?,?)' if band=='priority' else 'band IN (?,?,?)' if band=='meme_candidates' else 'band=?'
        args = () if band == 'all' else ('Borderline','Likely memes') if band=='priority' else ('Borderline','Likely memes','Memes') if band=='meme_candidates' else (band,)
        if people_state!='all':
            expression="COALESCE((SELECT json_extract(a.value,'$.state') FROM annotations a WHERE a.sha256=items.sha256 AND a.channel='people'),'unreviewed')"
            if people_state=='other':where+=" AND EXISTS(SELECT 1 FROM annotations a WHERE a.sha256=items.sha256 AND a.channel='people' AND json_extract(a.value,'$.other')=1)"
            else:where+=' AND '+expression+'=?';args+= (people_state,)
        if adult_state!='all':
            where+=" AND COALESCE((SELECT json_extract(a.value,'$.state') FROM annotations a WHERE a.sha256=items.sha256 AND a.channel='adult'),'unreviewed')=?";args+=(adult_state,)
        if adult_ml!='all':
            clauses={'flagged':'p.score>0.15','likely_adult':'p.score>=0.85','uncertain':'p.score>0.15 AND p.score<0.85','likely_safe':'p.score<=0.15','unscored':'1=1'}
            where+=(' AND NOT EXISTS' if adult_ml=='unscored' else ' AND EXISTS')+'(SELECT 1 FROM adult_predictions p WHERE p.sha256=items.sha256 AND '+clauses[adult_ml]+')'
        if persons:
            where+=' AND EXISTS(SELECT 1 FROM image_people p WHERE p.sha256=items.sha256 AND p.person IN ('+','.join('?' for p in persons)+'))';args+=tuple(persons)
        states = {'pending':'label IS NULL','skipped':"label='skip'",'decided':"label IN ('meme','not_meme')"}
        db = self.connect()
        try:
            db.execute('BEGIN')
            learning = self.learning_state(db)
            threshold = learning['model']['threshold'] if learning['active'] else 0.0
            if learning['active']:
                sorts['uncertain'] = 'ABS(score - '+repr(threshold)+'),sha256'
            def stats(clause, params=()):
                row = db.execute('SELECT COUNT(*) total,COALESCE(SUM(ready),0) ready,'
                    "COALESCE(SUM(label IN ('meme','not_meme')),0) reviewed,"
                    "COALESCE(SUM(label='skip'),0) skipped,COALESCE(SUM(label IS NULL),0) pending FROM items WHERE " + clause, params).fetchone()
                return dict(row)
            selected = stats(where, args)
            clause=states[state] if mode=='meme' else '1=1'
            queue_count = db.execute('SELECT COUNT(*) FROM items WHERE '+where+' AND ready=1 AND '+clause, args).fetchone()[0]
            rows = db.execute('SELECT * FROM items WHERE '+where+' AND ready=1 AND '+clause+' ORDER BY '+sorts[order]+' LIMIT 2 OFFSET ?', (*args,offset)).fetchall()
            undo = db.execute('SELECT e.id,e.sha256,e.label FROM events e JOIN items i ON i.sha256=e.sha256 '
                "WHERE e.kind='decision' AND i.revision=e.revision AND NOT EXISTS(SELECT 1 FROM events u WHERE u.undo_of=e.id) ORDER BY e.id DESC LIMIT 1").fetchone()
            annotation_undo={}
            for channel in ('people','adult'):
                event=db.execute("SELECT e.id,e.sha256 FROM annotation_events e JOIN annotations a ON a.sha256=e.sha256 AND a.channel=e.channel WHERE e.channel=? AND e.kind='decision' AND a.revision=e.revision AND NOT EXISTS(SELECT 1 FROM annotation_events u WHERE u.undo_of=e.id) ORDER BY e.id DESC LIMIT 1",(channel,)).fetchone()
                annotation_undo[channel]=dict(event) if event else None
            return {'items':[{**self.public(row),'people_review':self.annotation(db,row['sha256'],'people'),'adult_review':self.annotation(db,row['sha256'],'adult'),'adult_prediction':self.adult_prediction(db,row['sha256'])} for row in rows], 'selected':selected,
                'overall':stats('1=1'), 'bands':{b:stats('band=?',(b,))['total'] for b in CATALOG_BANDS},
                'people':[dict(r) for r in db.execute('SELECT id,name FROM people ORDER BY normalized')],
                'annotation_undo':annotation_undo,'mode':mode,'adult_ml':self.adult_prediction_stats(db),
                'queue_count':queue_count, 'offset':offset, 'undo':dict(undo) if undo else None,'learning':learning}
        finally: db.close()

    def decide(self, value, actor):
        if not isinstance(value, dict) or set(value) != {'op','sha256','label','revision'}:
            raise ValueError('Invalid decision.')
        if not REVIEW_OP.fullmatch(str(value['op'])) or not REVIEW_HASH.fullmatch(str(value['sha256'])):
            raise ValueError('Invalid decision identifier.')
        if value['label'] not in ('meme','not_meme','skip') or type(value['revision']) is not int or value['revision'] < 0:
            raise ValueError('Invalid decision label or revision.')
        return self.change(value, actor)

    def undo(self, value, actor):
        if not isinstance(value, dict) or set(value) != {'op','event'} or not REVIEW_OP.fullmatch(str(value['op'])) or type(value['event']) is not int:
            raise ValueError('Invalid undo.')
        return self.change(value, actor, undo=True)

    def change(self, value, actor, undo=False):
        request_hash = hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM events WHERE op=?', (value['op'],)).fetchone()
            if old:
                if old['request_hash'] != request_hash or old['kind'] != ('undo' if undo else 'decision'):
                    raise ReviewConflict('This request identifier was already used for a different action.')
                return {'saved':True,'event':old['id'],'sha256':old['sha256'],'label':old['label'],'revision':old['revision'],'replayed':True}
            source = db.execute('SELECT * FROM events WHERE id=?', (value['event'],)).fetchone() if undo else None
            if undo and (not source or source['kind'] != 'decision'):
                raise ValueError('Decision not found.')
            ident = source['sha256'] if undo else value['sha256']
            row = db.execute('SELECT * FROM items WHERE sha256=?', (ident,)).fetchone()
            if not row or not row['ready']: raise ValueError('Image preview is unavailable.')
            expected = source['revision'] if undo else value['revision']
            if row['revision'] != expected or undo and db.execute('SELECT 1 FROM events WHERE undo_of=?',(source['id'],)).fetchone():
                raise ReviewConflict('This image changed on another device. Refresh before trying again.')
            label = source['previous_label'] if undo else value['label']
            revision = row['revision'] + 1
            stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
            db.execute('UPDATE items SET label=?,revision=?,updated=? WHERE sha256=?', (label,revision,stamp,ident))
            result = db.execute('INSERT INTO events(op,request_hash,sha256,label,previous_label,revision,kind,undo_of,at,actor) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (value['op'],request_hash,ident,label,row['label'],revision,'undo' if undo else 'decision',source['id'] if undo else None,stamp,actor))
            db.commit()
            return {'saved':True,'event':result.lastrowid,'sha256':ident,'label':label,'revision':revision}
        finally: db.close()

    def export(self, format):
        db = self.connect()
        try:
            db.execute('BEGIN')
            items = [self.public(r) for r in db.execute('SELECT * FROM items WHERE revision>0 ORDER BY sha256')]
            if format == 'csv':
                out = io.StringIO(newline=''); writer = csv.DictWriter(out,fieldnames=['sha256','label','band','score','revision','updated'])
                writer.writeheader()
                for row in items:writer.writerow({k:row[k] for k in writer.fieldnames})
                return out.getvalue().encode('utf-8-sig'), 'text/csv;charset=utf-8', 'image-review-decisions.csv'
            events = [dict(r) for r in db.execute('SELECT id,sha256,label,previous_label,revision,kind,undo_of,at FROM events ORDER BY id')]
            body = {'schema':'mungoon-image-review-v1','exported_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
                'original_images_modified':False,'ranking_scores_are_probabilities':False,'items':items,'events':events,
                'traceability':'sha256 identifies the original in the existing organized_manifest.jsonl; events retain all label changes and undos.'}
            body['people']=[dict(r) for r in db.execute('SELECT id,name FROM people ORDER BY normalized')]
            body['annotations']=[{**dict(r),'value':json.loads(r['value'])} for r in db.execute('SELECT sha256,channel,value,revision,updated FROM annotations ORDER BY sha256,channel')]
            body['annotation_events']=[{**dict(r),'value':json.loads(r['value']),'previous_value':json.loads(r['previous_value'])} for r in db.execute('SELECT id,sha256,channel,value,previous_value,revision,kind,undo_of,at FROM annotation_events ORDER BY id')]
            body['adult_predictions']=[dict(r) for r in db.execute('SELECT sha256,media_sha,score,model,model_revision,updated FROM adult_predictions ORDER BY sha256')]
            return json.dumps(body,indent=2).encode(), 'application/json', 'image-review-history.json'
        finally: db.close()

def image_review_request(handler, store):
    # Defense in depth: no project grant can expose this personal collection.
    if not handler.identity.get('owner'): return handler.send_json({'error':'Owner access required.'},403)
    path = urlparse(handler.path).path
    prefix = '/image-review/api/'
    try:
        if handler.command == 'GET':
            if path == prefix+'learning':return handler.send_json(store.learning_status())
            if path == prefix+'queue': return handler.send_json(store.queue(parse_qs(urlparse(handler.path).query)))
            if path == prefix+'export':
                form = parse_qs(urlparse(handler.path).query).get('format',['json'])[0]
                if form not in ('json','csv'): raise ValueError('Invalid export format.')
                data, content, filename = store.export(form)
                handler.send_response(200); handler.send_header('Content-Type',content)
                handler.send_header('Content-Disposition','attachment; filename="'+filename+'"')
                handler.send_header('Content-Length',str(len(data))); handler.send_header('Cache-Control','no-store')
                handler.end_headers(); return handler.wfile.write(data)
            if path.startswith(prefix+'media/'):
                ident = path.removeprefix(prefix+'media/')
                if not REVIEW_HASH.fullmatch(ident):return handler.send_error(404)
                target = store.media / (ident+'.jpg')
                if not target.is_file():return handler.send_json({'error':'Preview is unavailable.'},404)
                return handler.send_file(target)
            return handler.send_error(404)
        if not handler.origin_ok(): return handler.reject_origin()
        n = int(handler.headers.get('Content-Length','0'))
        if not 0 < n <= 1800000 or handler.headers.get('Transfer-Encoding'):return handler.send_error(413)
        body = handler.rfile.read(n)
        if len(body) != n:raise ValueError('Incomplete request.')
        if path.startswith(prefix+'media/'):
            if not handler.headers.get('X-Project-Signature'):return handler.send_error(403)
            if handler.headers.get('Content-Type') != 'image/jpeg':return handler.send_error(415)
            return handler.send_json(store.import_media(path.removeprefix(prefix+'media/'),body))
        if handler.headers.get('Content-Type','').split(';')[0] != 'application/json':return handler.send_error(415)
        value = json.loads(body)
        if path == prefix+'adult/import-predictions':
            if not handler.headers.get('X-Project-Signature'):return handler.send_error(403)
            return handler.send_json(store.import_adult_predictions(value))
        if path == prefix+'people/create':return handler.send_json(store.create_person(value))
        for channel in ('people','adult'):
            if path == prefix+channel+'/decision':return handler.send_json(store.annotate(channel,value,handler.identity['email']))
            if path == prefix+channel+'/undo':return handler.send_json(store.annotate(channel,value,handler.identity['email'],undo=True))
        if path == prefix+'learning/train':
            if value != {}:raise ValueError('Invalid training request.')
            return handler.send_json(store.train())
        if path == prefix+'learning/predict':return handler.send_json(store.predict(value))
        if path == prefix+'import':
            if not handler.headers.get('X-Project-Signature'):return handler.send_error(403)
            return handler.send_json(store.import_items(value))
        if path == prefix+'decision':return handler.send_json(store.decide(value,handler.identity['email']))
        if path == prefix+'undo':return handler.send_json(store.undo(value,handler.identity['email']))
        return handler.send_error(404)
    except ReviewConflict as error:return handler.send_json({'error':str(error)},409)
    except (ValueError,TypeError,KeyError):return handler.send_json({'error':'Invalid review request.'},400)
    except Exception:return handler.send_json({'error':'Could not save or load review data. Retry shortly.'},503)
