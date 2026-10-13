"""Единый кэш произвольных интервалов; импорт старых месячных ответов без сети."""
from datetime import date,datetime,timedelta,timezone
import hashlib,json,math
from pathlib import Path
from urllib.parse import urlencode,urlparse,parse_qs
import numpy as np
from .meteo import connect,ENDPOINT,PARAMETERS
from .geography.grid import nearest_cell,cell_id


def dates(start,end):
    a=date.fromisoformat(str(start));b=date.fromisoformat(str(end))
    if a<date(2001,1,1) or b<a or b>=datetime.now(timezone.utc).date():
        raise ValueError('Нужны даты с 01.01.2001, начало ≤ конец, последний день — не позднее вчера (UTC)')
    return a,b


def url_for(cell,a,b):
    return ENDPOINT+'?'+urlencode(dict(parameters=','.join(PARAMETERS),community='RE',latitude=cell[3],longitude=cell[4],
                                      start=a.strftime('%Y%m%d'),end=b.strftime('%Y%m%d'),format='JSON',**{'time-standard':'UTC'}))


def db_open(folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    db=connect(folder/'meteo_cache.sqlite')
    db.execute('CREATE TABLE IF NOT EXISTS blocks(url TEXT PRIMARY KEY,cell TEXT,start TEXT,end TEXT)')
    db.execute('CREATE INDEX IF NOT EXISTS blocks_cell_date ON blocks(cell,start,end)')
    db.execute('CREATE TABLE IF NOT EXISTS block_quality(url TEXT PRIMARY KEY,complete INTEGER NOT NULL)')
    for (url,) in db.execute('SELECT url FROM responses WHERE url NOT IN (SELECT url FROM blocks)').fetchall():
        q=parse_qs(urlparse(url).query)
        if q.get('time-standard')!=['UTC'] or set(q.get('parameters',[''])[0].split(','))!=set(PARAMETERS):continue
        try:
            cid=cell_id(*nearest_cell(float(q['latitude'][0]),float(q['longitude'][0])))
            a=datetime.strptime(q['start'][0],'%Y%m%d').date();b=datetime.strptime(q['end'][0],'%Y%m%d').date()
            db.execute('INSERT INTO blocks VALUES (?,?,?,?)',(url,cid,a.isoformat(),b.isoformat()))
        except (KeyError,ValueError):continue
    db.commit();return db


def missing_ranges(db,cell,a,b,complete_only=False):
    cursor=a;result=[]
    for lo,hi in db.execute('SELECT start,end FROM blocks WHERE cell=? AND end>=? AND start<=? '
                           + ('AND url NOT IN (SELECT url FROM block_quality WHERE complete=0) ' if complete_only else '')
                           + 'ORDER BY start,end',
                           (cell,a.isoformat(),b.isoformat())):
        lo=date.fromisoformat(lo);hi=date.fromisoformat(hi)
        if lo>cursor:result.append((cursor,min(b,lo-timedelta(days=1))))
        cursor=max(cursor,hi+timedelta(days=1))
    if cursor<=b:result.append((cursor,b))
    return result


def year_chunks(a,b):
    while a<=b:
        end=min(b,date(a.year,12,31));yield a,end;a=end+timedelta(days=1)


def decode(raw,a,b):
    def pairs(items):
        result={}
        for k,v in items:
            if k in result:raise ValueError('Повторная метка/ключ NASA: '+k)
            result[k]=v
        return result
    data=json.loads(raw,object_pairs_hook=pairs);header=data.get('header',{})
    if header.get('time_standard')!='UTC':raise ValueError('Ответ не в UTC')
    params=data.get('properties',{}).get('parameter',{})
    total=((b-a).days+1)*24;array=np.full((total,2),np.nan)
    fill=header.get('fill_value',-999)
    for j,p in enumerate(PARAMETERS):
        if not isinstance(params.get(p),dict) or data.get('parameters',{}).get(p,{}).get('units')!='m/s':
            raise ValueError('Отсутствует параметр или неверные единицы '+p)
        for key,value in params[p].items():
            if len(key)!=10 or not key.isdigit():raise ValueError('Неверная временная метка '+key)
            t=datetime.strptime(key,'%Y%m%d%H')
            index=(t.date()-a).days*24+t.hour
            if not 0<=index<total:raise ValueError('Данные вне запрошенного диапазона')
            if type(value) in (int,float) and math.isfinite(value) and value>=0 and value!=fill:array[index,j]=value
    return array


def put(db,cell,a,b,raw):
    array=decode(raw,a,b);url=url_for(cell,a,b);sha=hashlib.sha256(raw).hexdigest()
    with db:
        db.execute('INSERT OR REPLACE INTO responses VALUES (?,?,?,?)',(url,datetime.now(timezone.utc).isoformat(),raw,sha))
        db.execute('INSERT OR REPLACE INTO blocks VALUES (?,?,?,?)',(url,cell[0],a.isoformat(),b.isoformat()))
        db.execute('INSERT OR REPLACE INTO block_quality VALUES (?,?)',(url,int(np.isfinite(array).all())))
    return int(np.isfinite(array).all(axis=1).sum()),len(array)


def _read_api_window(db,folder,cid,a,b,require_coverage=True):
    missing=missing_ranges(db,cid,a,b)
    if missing and require_coverage:raise ValueError(f'{cid}: не загружено {missing[0][0]} — {missing[0][1]}')
    result=np.full((((b-a).days+1)*24,2),np.nan);sources=[]
    entries=db.execute('''SELECT b.start,b.end,r.raw,r.sha256,r.url FROM blocks b JOIN responses r ON r.url=b.url
                         WHERE b.cell=? AND b.end>=? AND b.start<=? ORDER BY r.fetched_utc,b.start,b.end''',
                       (cid,a.isoformat(),b.isoformat()))
    # Валидированные числовые массивы ускоряют повторные расчёты; исходные ответы остаются первичными.
    decoded=Path(folder)/'decoded_v1';decoded.mkdir(exist_ok=True)
    for lo,hi,raw,sha,url in entries:
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError(f'{cid}: повреждён исходный кэш')
        lo=date.fromisoformat(lo);hi=date.fromisoformat(hi)
        key=hashlib.sha256((sha+str(lo)+str(hi)).encode()).hexdigest()
        binary=decoded/(key+'.npz');array=None
        if binary.exists():
            try:
                with np.load(binary,allow_pickle=False) as cache:
                    candidate=cache['wind']
                    if candidate.shape==(((hi-lo).days+1)*24,2) and hashlib.sha256(candidate.tobytes()).hexdigest()==str(cache['sha']):array=candidate
            except Exception:pass
        if array is None:
            array=decode(raw,lo,hi);np.savez_compressed(binary,wind=array,sha=hashlib.sha256(array.tobytes()).hexdigest())
        db.execute('INSERT OR REPLACE INTO block_quality VALUES (?,?)',(url,int(np.isfinite(array).all())))
        begin=max(a,lo);end=min(b,hi);dest=(begin-a).days*24;src=(begin-lo).days*24;n=((end-begin).days+1)*24
        existing=result[dest:dest+n];incoming=array[src:src+n]
        # При повторной загрузке более свежий ответ заменяет старый на пересечении.
        existing[:]=incoming
        sources.append({'url':url,'sha256':sha})
    db.commit();return result,sources


def read_window(db,folder,cid,a,b):
    from .cloud_archive import read_window as read_archive
    wind,sources=read_archive(folder,cid,a,b)
    if np.isfinite(wind).all():return wind,sources
    api,api_sources=_read_api_window(db,folder,cid,a,b,require_coverage=False)
    missing=~np.isfinite(wind);wind[missing]=api[missing]
    return wind,sources+api_sources


def inventory(cells,start,end,folder,include_archive=False):
    a,b=dates(start,end);tasks=[];ready=0;db=db_open(folder)
    try:
        for c in cells:
            missing=missing_ranges(db,c[0],a,b,complete_only=True)
            if include_archive:
                from .cloud_archive import covered_ranges
                for lo,hi in covered_ranges(folder,c[0],a,b):
                    rest=[]
                    for x,y in missing:
                        if hi<x or lo>y:rest.append((x,y));continue
                        if x<lo:rest.append((x,lo-timedelta(days=1)))
                        if y>hi:rest.append((hi+timedelta(days=1),y))
                    missing=rest
            if not missing:ready+=1
            for lo,hi in missing:
                tasks.extend((c,x,y) for x,y in year_chunks(lo,hi))
    finally:db.close()
    return tasks,ready
