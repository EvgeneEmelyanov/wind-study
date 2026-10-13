from datetime import date
from pathlib import Path
from threading import Event
import json
import numpy as np
import pytest
from zipfile import ZipFile
from windstudy import cloud_archive as cloud
from windstudy.weather_store import db_open,read_window,put
from windstudy.calculation import calculate
from windstudy.experiments import Model
from test_intervals import CELL,plan,raw

@pytest.fixture
def remote():
    # Эталон создан независимым писателем Zarr 2.18.7 до смены декодера.
    with ZipFile(Path(__file__).parent/'fixtures/cloud_zarr2.zip') as archive:
        return {name:archive.read(name) for name in archive.namelist()}


def getter(remote,calls):
    def get(url,cancel,on_retry):
        calls.append(url);return remote[url.removeprefix(cloud.BASE)]
    return get

def test_download_offline_calculation_resume_and_repair(tmp_path,remote,monkeypatch):
    calls=[];a=date(2023,1,1);b=date(2023,1,2)
    r=cloud.download([CELL],a,b,tmp_path,Event(),lambda *a:None,2,getter(remote,calls))
    assert r['status']=='complete',r['errors']
    assert r['completed_blocks']==4
    first=len(calls)
    r=cloud.download([CELL],a,b,tmp_path,Event(),lambda *a:None,2,getter(remote,calls))
    assert r['status']=='complete' and r['completed_blocks']==0 and len(calls)==first
    def forbidden(*a,**kw):raise AssertionError('Calculation must be offline')
    monkeypatch.setattr('windstudy.meteo.urlopen',forbidden)
    result=calculate(plan(),a,b,50,[Model(1)],tmp_path,tmp_path/'out',Event(),lambda *a:None)
    assert result['status']=='complete',result['errors']
    assert result['expected_hours']==48
    db=db_open(tmp_path);wind,sources=read_window(db,tmp_path,CELL[0],b,b);db.close()
    np.testing.assert_equal(wind[:,0],np.arange(24,48)%15+3)
    assert sources and all('local_sha256' in s for s in sources)
    broken=next((tmp_path/cloud.SUBDIR).glob('WS10M*.npz'));broken.write_bytes(b'broken')
    r=cloud.download([CELL],a,b,tmp_path,Event(),lambda *a:None,1,getter(remote,calls))
    assert r['status']=='complete' and r['completed_blocks']==1

def test_groups_neighbor_cells_once(tmp_path,remote):
    other=['M2:r322:c438',322,438,71,93.75,False];calls=[]
    r=cloud.download([CELL,other],date(2023,1,1),date(2023,1,1),tmp_path,Event(),lambda *a:None,1,getter(remote,calls))
    assert r['status']=='complete' and r['completed_blocks']==2
    assert len([u for u in calls if '/WS' in u])==2

def test_api_fills_only_unavailable_archive_hours(tmp_path,remote):
    a=date(2023,1,1);calls=[]
    r=cloud.download([CELL],a,a,tmp_path,Event(),lambda *a:None,1,getter(remote,calls))
    assert r['status']=='complete'
    db=db_open(tmp_path);put(db,CELL,date(2023,1,2),date(2023,1,2),raw(date(2023,1,2),date(2023,1,2)))
    wind,sources=read_window(db,tmp_path,CELL[0],a,date(2023,1,2));db.close()
    assert wind.shape==(48,2) and np.isfinite(wind).all()
    assert any(s['url'].startswith(cloud.BASE) for s in sources)
    assert any('api/temporal' in s['url'] for s in sources)

def test_cancel_and_metadata_reject(tmp_path,remote):
    event=Event();event.set()
    def get(*a,**kw):raise cloud.Cancelled()
    r=cloud.download([CELL],date(2023,1,1),date(2023,1,1),tmp_path,event,lambda *a:None,1,get)
    assert r['status']=='cancelled'
    meta=json.loads(remote['.zmetadata']);meta['metadata']['WS50M/.zattrs']['units']='km/h'
    with pytest.raises(ValueError):cloud.validate_metadata(meta)

def test_missing_cloud_object_is_not_silently_filled(tmp_path,remote):
    remote.pop('WS10M/0.64.87')
    r=cloud.download([CELL],date(2023,1,1),date(2023,1,1),tmp_path,Event(),lambda *a:None,1,getter(remote,[]))
    assert r['status']=='incomplete' and r['errors']

def test_atomic_write_retries_locked_destination(tmp_path,monkeypatch):
    target=tmp_path/'journal.json';target.write_bytes(b'old')
    original=Path.replace;attempts=[]
    monkeypatch.setattr(cloud.time,'sleep',lambda _:None)
    def replace(self,destination):
        attempts.append(self)
        if len(attempts)<3:raise PermissionError(13,'WinError 5: access denied')
        return original(self,destination)
    monkeypatch.setattr(Path,'replace',replace)
    cloud.atomic_bytes(target,b'new')
    assert target.read_bytes()==b'new' and len(attempts)==3
    assert not list(tmp_path.glob('*.tmp'))

def test_locked_journal_does_not_stop_download_or_resume(tmp_path,remote,monkeypatch):
    original=cloud.atomic_bytes;calls=[];messages=[]
    def write(path,raw):
        if Path(path).name.startswith('download_'):raise PermissionError(13,'WinError 5')
        return original(path,raw)
    monkeypatch.setattr(cloud,'atomic_bytes',write)
    a=date(2023,1,1);b=date(2023,1,2)
    r=cloud.download([CELL],a,b,tmp_path,Event(),lambda *args:messages.append(args),2,getter(remote,calls))
    assert r['status']=='complete' and r['completed_blocks']==4
    assert not r['journal_saved'] and r['warnings'] and not r['errors']
    count=len(calls)
    monkeypatch.setattr(cloud,'atomic_bytes',original)
    r=cloud.download([CELL],a,b,tmp_path,Event(),lambda *args:None,2,getter(remote,calls))
    assert r['status']=='complete' and r['already_present']==4 and len(calls)==count
    assert r['journal_saved'] and json.loads(Path(r['report_path']).read_text())['status']=='complete'

def test_block_write_failure_remains_fatal(tmp_path,remote,monkeypatch):
    original=Path.replace
    def replace(self,destination):
        if Path(destination).suffix=='.npz':raise PermissionError(13,'Block locked')
        return original(self,destination)
    monkeypatch.setattr(Path,'replace',replace)
    a=date(2023,1,1)
    r=cloud.download([CELL],a,a,tmp_path,Event(),lambda *args:None,1,getter(remote,[]))
    assert r['status']=='incomplete' and r['errors'] and r['completed_blocks']==0
