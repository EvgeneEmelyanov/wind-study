"""Локальный архив NASA POWER: облачные блоки читаются только загрузчиком.

Каждый Zarr-блок скачивается один раз. На диске — NPZ с отдельным сжатым
почасовым рядом каждой ячейки блока, чтобы расчёт не распаковывал соседей.
"""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
import hashlib
import json
import sqlite3
import re
import tempfile
import time
from functools import lru_cache
import numpy as np
from .meteo import fetch, Cancelled, PARAMETERS
from .request_gate import RequestGate

BASE='https://nasa-power.s3.us-west-2.amazonaws.com/merra2/temporal/power_merra2_hourly_temporal_utc.zarr/'
SUBDIR='power_archive_v1'

def digest(raw):return hashlib.sha256(raw).hexdigest()

def atomic_bytes(path,raw):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,prefix=path.name+'.',suffix='.tmp',delete=False) as f:
            tmp=Path(f.name);f.write(raw)
        # Windows может ненадолго блокировать замену открытого файла.
        for attempt in range(5):
            try:
                tmp.replace(path);break
            except PermissionError:
                if attempt==4:raise
                time.sleep(.1*2**attempt)
    finally:
        if tmp is not None:
            try:tmp.unlink(missing_ok=True)
            except OSError:pass

def connect(folder):
    path=Path(folder)/SUBDIR;path.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path/'index.sqlite')
    db.execute('CREATE TABLE IF NOT EXISTS chunks (key TEXT PRIMARY KEY,sha TEXT,source_sha TEXT,size INTEGER,downloaded TEXT)')
    return path,db

def metadata(folder):
    p=Path(folder)/SUBDIR/'metadata.json'
    if not p.exists():return None
    stat=p.stat();return _metadata(str(p),stat.st_mtime_ns,stat.st_size)

@lru_cache(maxsize=4)
def _metadata(path,mtime,size):
    raw=Path(path).read_bytes();data=json.loads(raw);validate_metadata(data)
    data['_snapshot_sha256']=digest(raw);return data

def validate_metadata(data):
    m=data['metadata']
    if 'UTC' not in m['.zattrs'].get('time_standard',''):raise ValueError('Облачный архив не подтверждает UTC')
    if m['time/.zattrs'].get('calendar') not in ('proleptic_gregorian','standard','gregorian'):raise ValueError('Неизвестный календарь NASA')
    epoch(data)
    if m['lat/.zarray']['shape']!=[361] or m['lon/.zarray']['shape']!=[576]:raise ValueError('Изменилась сетка NASA')
    for p in PARAMETERS:
        attrs=m[p+'/.zattrs'];spec=m[p+'/.zarray']
        if attrs['units']!='m/s' or attrs['_ARRAY_DIMENSIONS']!=['time','lat','lon']:raise ValueError('Неизвестные единицы или порядок осей '+p)
        if spec['zarr_format']!=2 or spec['dimension_separator']!='.' or spec['shape'][1:]!=[361,576]:raise ValueError('Неподдерживаемая структура Zarr')
        if np.prod(spec['chunks'])*np.dtype(spec['dtype']).itemsize>64*1024**2:raise ValueError('Слишком большой блок NASA; требуется обновить загрузчик')
    if m['WS10M/.zarray']['chunks']!=m['WS50M/.zarray']['chunks']:raise ValueError('Несовместимые блоки скоростей')

def epoch(data):
    units=data['metadata']['time/.zattrs']['units']
    if not units.startswith('hours since '):raise ValueError('Ожидалась почасовая ось времени')
    value=datetime.fromisoformat(units[len('hours since '):])
    if value.tzinfo:value=value.astimezone(timezone.utc).replace(tzinfo=None)
    if value.time()!=datetime.min.time():raise ValueError('Начало оси времени не в 00:00')
    return value

def bounds(data,a,b):
    origin=epoch(data);start=int((datetime.combine(a,datetime.min.time())-origin).total_seconds()//3600)
    end=int((datetime.combine(b+timedelta(days=1),datetime.min.time())-origin).total_seconds()//3600)
    if start<0 or end>data['metadata']['WS10M/.zarray']['shape'][0]:raise ValueError('Даты за пределами оси облачного архива')
    return start,end

def decode_chunk(data,name,key,raw):
    # Читаем только нужный числовой блок Zarr v2; библиотека zarr не требуется.
    from numcodecs import get_codec
    spec=data['metadata'][name+'/.zarray'];index=tuple(map(int,key.split('.')))
    chunks=tuple(spec['chunks']);shape=tuple(spec['shape']);dtype=np.dtype(spec['dtype'])
    if len(index)!=len(chunks) or len(shape)!=len(chunks) or dtype.kind not in 'fi':
        raise ValueError('Неподдерживаемый числовой блок NASA')
    if any(i<0 or i*c>=n for i,c,n in zip(index,chunks,shape)):
        raise ValueError('Индекс блока вне массива NASA')
    if spec.get('order') not in ('C','F'):raise ValueError('Неизвестный порядок элементов')
    compressor=spec.get('compressor');buffer=raw
    if compressor:
        if compressor.get('id')!='blosc':raise ValueError('Неизвестное сжатие NASA')
        buffer=get_codec(compressor).decode(buffer)
    for f in reversed(spec.get('filters') or []):
        if f.get('id')!='fixedscaleoffset':raise ValueError('Неизвестный фильтр NASA')
        buffer=get_codec(f).decode(buffer)
    array=np.frombuffer(buffer,dtype=dtype)
    if array.size!=int(np.prod(chunks)):raise ValueError('Неверный размер распакованного блока NASA')
    array=array.reshape(chunks,order=spec['order'])
    slices=tuple(slice(0,min(c,n-i*c)) for i,c,n in zip(index,chunks,shape))
    return np.asarray(array[slices])


def initialize(folder,cancel,progress,downloader):
    current=metadata(folder)
    if current is not None:return current
    progress(0,1,'Архив NASA: проверка метаданных и координат…')
    raw=downloader(BASE+'.zmetadata',cancel,on_retry=lambda m:progress(0,1,m))
    data=json.loads(raw);validate_metadata(data)
    for name,expected in [('lat',np.arange(361)*.5-90),('lon',np.arange(576)*.625-180)]:
        progress(0,1,'Архив NASA: проверка координаты '+name)
        spec=data['metadata'][name+'/.zarray'];parts=[]
        for i in range((spec['shape'][0]+spec['chunks'][0]-1)//spec['chunks'][0]):
            payload=downloader(BASE+name+'/'+str(i),cancel,on_retry=lambda m:progress(0,1,m))
            parts.append(decode_chunk(data,name,str(i),payload))
        if not np.allclose(np.concatenate(parts),expected,rtol=0,atol=1e-9):raise ValueError('Координаты NASA не совпадают с сеткой пула')
    # Проверяем реальную временную координату, а не только описание оси.
    spec=data['metadata']['time/.zarray']
    for i in range((spec['shape'][0]+spec['chunks'][0]-1)//spec['chunks'][0]):
        progress(i,((spec['shape'][0]+spec['chunks'][0]-1)//spec['chunks'][0]),'Архив NASA: проверка часовой оси')
        payload=downloader(BASE+'time/'+str(i),cancel,on_retry=lambda m:progress(0,1,m))
        values=decode_chunk(data,'time',str(i),payload);start=i*spec['chunks'][0]
        if not np.array_equal(values,np.arange(start,start+len(values))):raise ValueError('Временная ось NASA не является непрерывной почасовой')
    atomic_bytes(Path(folder)/SUBDIR/'metadata.json',raw)
    return data

def chunk_keys(data,cells,a,b):
    start,end=bounds(data,a,b);ct,cy,cx=data['metadata']['WS10M/.zarray']['chunks']
    tiles=set()
    for c in cells:
        if c[0]!=f'M2:r{c[1]:03d}:c{c[2]:03d}' or c[3]!=-90+c[1]*.5 or c[4]!=-180+c[2]*.625:raise ValueError('Ячейка не соответствует сетке MERRA-2')
        tiles.add((c[1]//cy,c[2]//cx))
    return [f'{p}/{t}.{y}.{x}' for y,x in sorted(tiles) for t in range(start//ct,(end-1)//ct+1) for p in PARAMETERS]

def filename(root,key):return root/(key.replace('/','_')+'.npz')

def records(folder):
    path=Path(folder)/SUBDIR/'index.sqlite'
    if not path.exists():return {}
    stat=path.stat();return _records(str(path),stat.st_mtime_ns,stat.st_size)

@lru_cache(maxsize=4)
def _records(path,mtime,size):
    with sqlite3.connect(path) as db:return {r[0]:r[1:] for r in db.execute('SELECT key,sha,source_sha,size,downloaded FROM chunks')}

def covered_ranges(folder,cid,a,b):
    data=metadata(folder)
    if data is None:return []
    match=re.fullmatch(r'M2:r(\d+):c(\d+)',cid)
    if not match:return []
    row,col=map(int,match.groups());ct,cy,cx=data['metadata']['WS10M/.zarray']['chunks'];start,end=bounds(data,a,b)
    root=Path(folder)/SUBDIR;index=records(folder);ranges=[]
    for t in range(start//ct,(end-1)//ct+1):
        keys=[f'{p}/{t}.{row//cy}.{col//cx}' for p in PARAMETERS]
        if all(k in index and filename(root,k).exists() and filename(root,k).stat().st_size==index[k][2] for k in keys):
            lo=epoch(data)+timedelta(hours=max(start,t*ct));hi=epoch(data)+timedelta(hours=min(end,(t+1)*ct)-1)
            ranges.append((lo.date(),hi.date()))
    return ranges

def read_window(folder,cid,a,b):
    total=((b-a).days+1)*24;result=np.full((total,2),np.nan);sources=[]
    data=metadata(folder)
    if data is None:return result,sources
    match=re.fullmatch(r'M2:r(\d+):c(\d+)',cid)
    if not match:raise ValueError('Неверный ID '+cid)
    row,col=map(int,match.groups());ct,cy,cx=data['metadata']['WS10M/.zarray']['chunks'];start,end=bounds(data,a,b)
    root=Path(folder)/SUBDIR;index=records(folder)
    for j,p in enumerate(PARAMETERS):
        for t in range(start//ct,(end-1)//ct+1):
            key=f'{p}/{t}.{row//cy}.{col//cx}';path=filename(root,key)
            if key not in index or not path.exists():continue
            field=f'r{row%cy}_c{col%cx}'
            with np.load(path,allow_pickle=False) as cache:
                values=cache[field]
                if digest(values.tobytes())!=str(cache['sha_'+field]):raise ValueError('Повреждён локальный архив: '+key)
            expected=min(ct,data['metadata'][p+'/.zarray']['shape'][0]-t*ct)
            if values.shape!=(expected,):raise ValueError('Неверная длина локального ряда: '+key)
            lo=max(start,t*ct);hi=min(end,(t+1)*ct)
            result[lo-start:hi-start,j]=values[lo-t*ct:hi-t*ct]
            sources.append({'url':BASE+key,'sha256':index[key][1],'local_sha256':index[key][0],'metadata_sha256':data['_snapshot_sha256']})
    result[~np.isfinite(result)|(result<0)]=np.nan
    return result,sources

def download(cells,start,end,folder,cancel,progress,workers=3,downloader=None):
    from .weather_store import dates
    a,b=dates(start,end);gate=RequestGate(workers,spacing=.2)
    if downloader is None:downloader=lambda url,cancel,on_retry:fetch(url,cancel,on_retry,gate=gate)
    root,db=connect(folder);report={'status':'running','source':'NASA POWER AWS Zarr UTC','cells':len(cells),'start':str(a),'end':str(b),'completed_blocks':0,'errors':[],'quality_gaps':[],'downloaded_bytes':0}
    log=root/('download_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.json');report['report_path']=str(log)
    report.update(journal_saved=False,warnings=[])
    next_log=0.0
    def save_log(force=False):
        nonlocal next_log
        if not force and time.monotonic()<next_log:return
        next_log=time.monotonic()+30
        try:
            snapshot=dict(report,journal_saved=True)
            atomic_bytes(log,json.dumps(snapshot,ensure_ascii=False,indent=2).encode())
            report['journal_saved']=True
        except OSError as error:
            report['journal_saved']=False
            warning='Не удалось обновить журнал загрузки; блоки сохраняются отдельно. '+str(error)
            # Ошибка вспомогательного журнала не отменяет записи в индексе архива.
            if not report['warnings']:
                report['warnings'].append(warning)
                progress(report['completed_blocks'],max(1,report.get('planned_requests',1)),warning)
    try:
        had_metadata=metadata(folder) is not None
        data=initialize(folder,cancel,progress,downloader);keys=chunk_keys(data,cells,a,b);existing=records(folder);todo=[]
        for i,key in enumerate(keys):
            if cancel.is_set():raise Cancelled()
            path=filename(root,key)
            if i%100==0:progress(i,len(keys),'Архив: проверка сохранённых блоков')
            if key in existing and path.exists() and path.stat().st_size==existing[key][2] and digest(path.read_bytes())==existing[key][0]:continue
            todo.append(key)
        if had_metadata and todo:
            fresh=json.loads(downloader(BASE+'.zmetadata',cancel,on_retry=lambda m:progress(0,len(todo),m)))
            validate_metadata(fresh)
            for name in (*PARAMETERS,'time','lat','lon'):
                for suffix in ('/.zarray','/.zattrs'):
                    key=name+suffix
                    if data['metadata'].get(key)!=fresh['metadata'].get(key):
                        raise ValueError('NASA изменила структуру архива. Выберите новую папку кэша; старый архив сохранён.')
        report.update(planned_requests=len(todo),already_present=len(keys)-len(todo),total_blocks=len(keys))
        progress(0,max(1,len(todo)),f'Архив: нужно скачать {len(todo)} блоков, сохранено {len(keys)-len(todo)}')
        def request(key):
            payload=downloader(BASE+key,cancel,on_retry=lambda m:progress(report['completed_blocks'],max(1,len(todo)),m))
            if cancel.is_set():raise Cancelled()
            p,k=key.split('/');values=decode_chunk(data,p,k,payload);arrays={}
            for y in range(values.shape[1]):
                for x in range(values.shape[2]):
                    name=f'r{y}_c{x}';v=np.ascontiguousarray(values[:,y,x]);arrays[name]=v;arrays['sha_'+name]=np.array(digest(v.tobytes()))
            path=filename(root,key);tmp=path.with_suffix('.tmp')
            try:
                with tmp.open('wb') as f:np.savez_compressed(f,**arrays)
                sha=digest(tmp.read_bytes());size=tmp.stat().st_size;tmp.replace(path)
            finally:tmp.unlink(missing_ok=True)
            return sha,digest(payload),size,len(payload)
        iterator=iter(todo);pending={};halt=False;done=0
        with ThreadPoolExecutor(max_workers=max(1,min(5,workers))) as executor:
            def fill():
                while not halt and not cancel.is_set() and len(pending)<max(1,min(5,workers)):
                    key=next(iterator,None)
                    if key is None:break
                    pending[executor.submit(request,key)]=key
            fill()
            while pending:
                finished,_=wait(pending,timeout=.2,return_when=FIRST_COMPLETED)
                for future in finished:
                    key=pending.pop(future)
                    try:
                        sha,source_sha,size,received=future.result()
                        with db:db.execute('INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?)',(key,sha,source_sha,size,datetime.now(timezone.utc).isoformat()))
                        report['completed_blocks']+=1;report['downloaded_bytes']+=received
                    except Cancelled:pass
                    except Exception as error:
                        report['errors'].append({'cell':key,'error':str(error)});halt=True
                    done+=1;progress(done,max(1,len(todo)),f'Архив: {done}/{len(todo)}; получено {report["downloaded_bytes"]/1024**2:.1f} МиБ')
                    save_log()
                fill()
        report['status']='cancelled' if cancel.is_set() else ('incomplete' if report['errors'] else 'complete')
    except Cancelled:report['status']='cancelled'
    except Exception as error:report['status']='incomplete';report['errors'].append({'cell':'archive','error':str(error)})
    finally:
        db.close();save_log(force=True)
    return report
