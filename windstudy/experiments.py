"""Эксперименты, повторное использование погоды и статистика по моделям."""
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import numpy as np
from .sampling import generate
from .approximation import CurveSettings, normalized_power, FORMULA_VERSION
from .meteo import connect, request_url, parse_month, fetch, Cancelled

@dataclass(frozen=True)
class Model:
    exponent: int
    v_min: float = 3
    v_nom: float = 12.5
    v_max: float = 25
    nominal_kw: float = 1000

    def validate(self):
        CurveSettings(self.v_min,self.v_nom,self.v_max).validate()
        if self.exponent not in (1,2,3) or not np.isfinite(self.nominal_kw) or self.nominal_kw<=0:
            raise ValueError('Неверная степень или номинальная мощность')


def atomic_json(path, value):
    path=Path(path); tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8');tmp.replace(path)


def make_plan(pool,n,repetitions,seed,cancel,progress):
    if not 1<=n<=len(pool.cells) or not 1<=repetitions<=10000 or seed+repetitions>2**32:
        raise ValueError('Неверные N, R или seed')
    # Хеш большого географического паспорта рассчитывается один раз.
    class View:
        cells=pool.cells
        checksum=pool.checksum
    plans=[];counts=Counter();coverage=[]
    for e in range(repetitions):
        if cancel.is_set(): raise Cancelled()
        sample=generate(View(),n,seed+e)
        ids=[c[0] for c in sample['cells']];plans.append({'experiment':e+1,'seed':seed+e,'ids':ids})
        counts.update(ids);coverage.append(len(counts));progress(e+1,repetitions,'Формирование экспериментов')
    cells=[c for c in pool.cells if c[0] in counts]
    return {'n':n,'repetitions':repetitions,'base_seed':seed,'pool_size':len(pool.cells),
            'pool_checksum':View.checksum,'plans':plans,'cells':cells,'frequency':dict(counts),
            'coverage':coverage,'sampling':sample['parameters'],'runtime':sample['runtime']}


def cache_counts(plan,year,folder,cancel,progress):
    path=Path(folder);path.mkdir(parents=True,exist_ok=True)
    ready=set();months=set()
    with connect(path/'meteo_cache.sqlite') as db:
        entries=db.execute('SELECT url FROM responses').fetchall()
        months={r[0] for r in entries}
    # Здесь проверяется наличие ответов, а не качество рядов: это явно отражено в UI.
    for c in plan['cells']:
        if all(request_url(c,year,m) in months for m in range(1,13)):ready.add(c[0])
    from urllib.parse import urlparse,parse_qs
    yearly={}
    for url in months:
        q=parse_qs(urlparse(url).query)
        start=q.get('start',[''])[0]
        if len(start)==8 and start.startswith(str(year)) and q.get('time-standard')==['UTC']:
            key=(q.get('latitude',[''])[0],q.get('longitude',[''])[0])
            yearly.setdefault(key,set()).add(start[4:6])
    all_cached=sum(len(v)==12 for v in yearly.values())
    plan['cache_snapshot']={'year':year,'selected_with_12_responses':len(ready),
                            'all_with_12_responses':all_cached,'quality_verified':False}
    return len(ready),sum(request_url(c,year,m) not in months for c in plan['cells'] for m in range(1,13))


def statistics(values):
    a=np.asarray(values,dtype=float)
    return {'count':len(a),'mean':float(a.mean()),'median':float(np.median(a)),
            'min':float(a.min()),'max':float(a.max()),'range':float(np.ptp(a)),
            'variance_sample':float(a.var(ddof=1)) if len(a)>1 else None,
            'std_sample':float(a.std(ddof=1)) if len(a)>1 else None,
            'q05':float(np.quantile(a,.05)),'q25':float(np.quantile(a,.25)),
            'q75':float(np.quantile(a,.75)),'q95':float(np.quantile(a,.95)),
            'iqr':float(np.quantile(a,.75)-np.quantile(a,.25))}


def wind_at_height(wind,height):
    a=np.asarray(wind,dtype=float);v=a[:,1].copy()
    if height==50:
        valid=np.isfinite(v)&(v>=0)
    else:
        valid=np.all(np.isfinite(a)&(a>0),axis=1)
        if valid.all():
            with np.errstate(over='ignore',invalid='ignore'):
                alpha=np.log(a[:,1]/a[:,0])/np.log(5)
                v=a[:,1]*(height/50)**alpha
            valid &= np.isfinite(v)&(v>=0)
    return v,valid


def run_experiments(plan,year,height,models,folder,output,cancel,progress,downloader=fetch):
    if not 2001<=year<datetime.now(timezone.utc).year:raise ValueError('Нужен завершённый год с 2001')
    if not np.isfinite(height) or height<=0:raise ValueError('Высота должна быть положительной')
    for m in models:m.validate()
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    atomic_json(output/'plan.json',plan)
    report={'status':'running','year':year,'height_m':height,'models':[asdict(m) for m in models],
            'formula':FORMULA_VERSION,'cells':[],'errors':[],'source':'NASA POWER, UTC',
            'height_policy':'50m direct; otherwise hourly alpha; zero/nonpositive inputs rejected; no alpha clipping',
            'density_correction':'none','started_utc':datetime.now(timezone.utc).isoformat()}
    calculated={};total=len(plan['cells'])*12;done=0
    db=connect(folder/'meteo_cache.sqlite')
    try:
        if cancel.is_set():raise Cancelled()
        for cell in plan['cells']:
            if cancel.is_set():raise Cancelled()
            wind=[];hashes=[];quality=[]
            for month in range(1,13):
                if cancel.is_set():raise Cancelled()
                url=request_url(cell,year,month);progress(done,total,f'{cell[0]} · {year}-{month:02d}')
                saved=db.execute('SELECT raw,sha256 FROM responses WHERE url=?',(url,)).fetchone()
                if saved:
                    raw,sha=saved
                    if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError(f'Повреждение кэша: {cell[0]}, {month}')
                else:
                    if downloader is fetch:
                        raw=fetch(url,cancel,on_retry=lambda message: progress(done,total,
                            f'{cell[0]} · {year}-{month:02d} · {message}'))
                    else:
                        raw=downloader(url,cancel)
                    parse_month(raw,year,month);sha=hashlib.sha256(raw).hexdigest()
                    with db:db.execute('INSERT OR REPLACE INTO responses VALUES (?,?,?,?)',(url,datetime.now(timezone.utc).isoformat(),raw,sha))
                    if cancel.wait(.25):raise Cancelled()
                rows,q=parse_month(raw,year,month);hashes.append(sha);quality.append(q)
                wind.extend([[r[1] if r[1] is not None else np.nan,r[2] if r[2] is not None else np.nan] for r in rows])
                done+=1
            v,valid=wind_at_height(wind,height)
            if not valid.all():
                raise ValueError(f"{cell[0]}: {np.sum(~valid)} непригодных часов; серия остановлена. Измените год/высоту или проверьте данные; ячейки не заменены")
            result=[]
            for m in models:
                p=m.power(v) if hasattr(m,'power') else normalized_power(v,m.exponent,CurveSettings(m.v_min,m.v_nom,m.v_max))
                result.append({'cf':float(p.mean()),'hours_equivalent':float(p.sum()),'energy_kwh':float(p.sum()*m.nominal_kw)})
            calculated[cell[0]]=result
            report['cells'].append({'id':cell[0],'lat':cell[3],'lon':cell[4],'hours':len(v),
                                    'monthly_sha256':hashes,'quality':quality,'results':result})
            if len(report['cells'])%25==0:atomic_json(output/'result.json',report)
        means=[];medians=[]
        for exp in plan['plans']:
            if cancel.is_set():raise Cancelled()
            matrix=np.array([[r['cf'] for r in calculated[cid]] for cid in exp['ids']])
            means.append(matrix.mean(axis=0).tolist());medians.append(np.median(matrix,axis=0).tolist())
        report.update(status='complete',means=means,medians=medians,
                      statistics={key:[statistics(np.array(values)[:,j]) for j in range(len(models))]
                                  for key,values in [('means',means),('medians',medians)]})
        progress(total,total,'Расчёт завершён')
    except Cancelled:report['status']='cancelled'
    except Exception as error:report['status']='incomplete';report['errors'].append(str(error))
    finally:db.close();atomic_json(output/'result.json',report)
    return report
