from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Event, Lock
from urllib.parse import urlparse, parse_qs
import json
import shutil
import time
import numpy as np
import pytest
from windstudy.weather_store import dates, db_open, put, decode, read_window, inventory, year_chunks
from windstudy.downloading import download
from windstudy.calculation import calculate
from windstudy.experiments import Model
from windstudy.excel import export_report

ROOT=Path(__file__).resolve().parents[1]
CELL=['M2:r322:c437',322,437,71,93.125,False]

def raw(a,b,missing=None):
    times=[datetime(a.year,a.month,a.day)+timedelta(hours=i) for i in range(((b-a).days+1)*24)]
    values={t.strftime('%Y%m%d%H'):float(3+(i%15)) for i,t in enumerate(times)}
    if missing:values.pop(missing,None)
    return json.dumps({'header':{'time_standard':'UTC','fill_value':-999},
        'properties':{'parameter':{'WS10M':values,'WS50M':values}},
        'parameters':{p:{'units':'m/s'} for p in ('WS10M','WS50M')}}).encode()

def plan():
    return {'n':1,'repetitions':2,'pool_size':1,'pool_checksum':'test','base_seed':42,
            'cells':[CELL],'frequency':{CELL[0]:2},'coverage':[1,1],
            'plans':[{'experiment':i+1,'seed':42+i,'ids':[CELL[0]]} for i in range(2)]}

def test_inclusive_leap_window_and_corrupt_decoded_cache(tmp_path):
    a,b=date(2020,1,1),date(2020,12,31);payload=raw(a,b);db=db_open(tmp_path)
    assert put(db,CELL,a,b,payload)==(8784,8784)
    x,sources=read_window(db,tmp_path,CELL[0],date(2020,2,28),date(2020,3,1))
    assert x.shape==(72,2)
    full=decode(payload,a,b);np.testing.assert_equal(x,full[58*24:61*24])
    next((tmp_path/'decoded_v1').glob('*.npz')).write_bytes(b'broken')
    y,_=read_window(db,tmp_path,CELL[0],date(2020,2,28),date(2020,3,1))
    np.testing.assert_equal(y,x);assert len(sources)==1;db.close()

def test_old_months_reused_without_requests(tmp_path):
    shutil.copy(ROOT/'cache/meteo_cache.sqlite',tmp_path)
    tasks,ready=inventory([CELL],'2023-02-15','2023-03-02',tmp_path)
    assert not tasks and ready==1
    db=db_open(tmp_path);wind,_=read_window(db,tmp_path,CELL[0],date(2023,2,15),date(2023,3,2))
    assert wind.shape==(16*24,2) and np.isfinite(wind).all();db.close()

def test_year_chunks_and_only_missing_dates(tmp_path):
    db=db_open(tmp_path);a,b=date(2020,2,1),date(2020,11,30);put(db,CELL,a,b,raw(a,b));db.close()
    tasks,_=inventory([CELL],'2019-12-30','2021-01-02',tmp_path)
    assert [(a,b) for _,a,b in tasks]==[(date(2019,12,30),date(2019,12,31)),(date(2020,1,1),date(2020,1,31)),(date(2020,12,1),date(2020,12,31)),(date(2021,1,1),date(2021,1,2))]
    with pytest.raises(ValueError):dates('2000-01-01','2020-01-01')
    with pytest.raises(ValueError):dates('2020-01-02','2020-01-01')

def test_offline_calculation_exact_hours_and_excel(tmp_path,monkeypatch):
    cache=tmp_path/'cache';cache.mkdir();shutil.copy(ROOT/'cache/meteo_cache.sqlite',cache)
    def no_network(*a,**kw):raise AssertionError('Network forbidden during calculation')
    monkeypatch.setattr('windstudy.meteo.urlopen',no_network)
    report=calculate(plan(),'2023-02-15','2023-03-02',50,[Model(1),Model(2),Model(3)],cache,tmp_path/'out',Event(),lambda *a:None)
    assert report['status']=='complete',report['errors']
    assert report['expected_hours']==384 and report['cells'][0]['hours']==384
    assert len(report['means'])==len(report['medians'])==2
    assert report['statistics']['means'][0]['std_sample']==0
    db=db_open(cache);wind,_=read_window(db,cache,CELL[0],date(2023,2,15),date(2023,3,2));db.close()
    from windstudy.approximation import normalized_power,CurveSettings
    expected=normalized_power(wind[:,1],1,CurveSettings()).mean()
    assert report['means'][0][0]==pytest.approx(expected)
    export_report(plan(),report,tmp_path/'out/report.xlsx')
    import openpyxl
    wb=openpyxl.load_workbook(tmp_path/'out/report.xlsx',read_only=True)
    assert wb['Средние']['C2'].value==pytest.approx(expected)
    assert wb['Сводка']['A3'].value=='Период UTC';wb.close()

def test_no_model_calculation_until_all_hours_available(tmp_path,monkeypatch):
    db=db_open(tmp_path);a=b=date(2023,1,1);put(db,CELL,a,b,raw(a,b,missing='2023010123'));db.close()
    def forbidden(*args):raise AssertionError('Model must not run')
    monkeypatch.setattr('windstudy.calculation.normalized_power',forbidden)
    result=calculate(plan(),a,b,50,[Model(1)],tmp_path,tmp_path/'out',Event(),lambda *a:None)
    assert result['status']=='incomplete' and not result['cells'] and 'means' not in result
    assert '1 непригодных часов' in result['errors'][0]
    tasks,_=inventory([CELL],a,b,tmp_path);assert len(tasks)==1

def test_download_parallel_years_and_resume(tmp_path):
    calls=[];lock=Lock();active=0;maximum=0
    def fake(url,cancel,on_retry):
        nonlocal active,maximum
        q=parse_qs(urlparse(url).query);a=datetime.strptime(q['start'][0],'%Y%m%d').date();b=datetime.strptime(q['end'][0],'%Y%m%d').date()
        with lock:active+=1;maximum=max(maximum,active);calls.append((a,b))
        time.sleep(.03);payload=raw(a,b)
        with lock:active-=1
        return payload
    r=download([CELL],'2020-01-01','2022-12-31',tmp_path,Event(),lambda *a:None,3,fake)
    assert r['status']=='complete' and len(calls)==3 and 1<maximum<=3
    r=download([CELL],'2021-01-01','2021-12-31',tmp_path,Event(),lambda *a:None,3,fake)
    assert r['status']=='complete' and r['completed_blocks']==0 and len(calls)==3

def test_adaptive_split_and_cancel(tmp_path):
    def fake(url,cancel,on_retry):
        q=parse_qs(urlparse(url).query);a=datetime.strptime(q['start'][0],'%Y%m%d').date();b=datetime.strptime(q['end'][0],'%Y%m%d').date()
        if (b-a).days>100:raise ConnectionError('simulated large request failure')
        return raw(a,b)
    r=download([CELL],'2023-01-01','2023-12-31',tmp_path,Event(),lambda *a:None,2,fake)
    assert r['status']=='complete' and r['completed_blocks']==4
    e=Event();e.set()
    r=download([CELL],'2022-01-01','2022-12-31',tmp_path,e,lambda *a:None,2,fake)
    assert r['status']=='cancelled' and r['completed_blocks']==0

def test_incomplete_download_can_be_repaired(tmp_path):
    a=b=date(2023,1,1)
    def missing(*args,**kwargs):return raw(a,b,missing='2023010100')
    r=download([CELL],a,b,tmp_path,Event(),lambda *a:None,1,missing)
    assert r['status']=='incomplete' and len(r['quality_gaps'])==1
    def full(*args,**kwargs):return raw(a,b)
    r=download([CELL],a,b,tmp_path,Event(),lambda *a:None,1,full)
    assert r['status']=='complete' and r['completed_blocks']==1
    assert not inventory([CELL],a,b,tmp_path)[0]
