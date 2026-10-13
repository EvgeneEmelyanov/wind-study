from datetime import date
from threading import Event
from urllib.error import HTTPError
import pytest
from windstudy.request_gate import RequestGate
from windstudy import meteo
from windstudy.workflow import run
from windstudy.experiments import Model
from test_intervals import plan,raw

class Clock:
    def __init__(self):self.now=0.;self.stop=False
    def __call__(self):return self.now
    def is_set(self):return self.stop
    def wait(self,seconds):self.now+=seconds;return self.stop

class Response:
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def read(self):return b'ok'

def test_global_429_pause_and_serial_resume(monkeypatch):
    clock=Clock();gate=RequestGate(3,clock=clock);starts=[];messages=[]
    monkeypatch.setattr('windstudy.request_gate.random.uniform',lambda *a:1)
    def request(*a,**kw):
        starts.append(clock.now)
        if len(starts)<=2:raise HTTPError('url',429,'limit',{'Retry-After':'90'},None)
        return Response()
    monkeypatch.setattr(meteo,'urlopen',request)
    assert meteo.fetch('url',clock,messages.append,gate)==b'ok'
    assert starts[1]>=91 and starts[2]-starts[1]>=121
    assert gate.limit==1 and gate.active==0 and messages
    # Другой работник той же очереди также проходит через общую паузу.
    gate.penalize(180);before=clock.now
    gate.acquire(clock,messages.append)
    assert clock.now-before>=181;gate.release()

def test_cancel_during_shared_pause():
    clock=Clock();gate=RequestGate(clock=clock);gate.penalize()
    def stop(message):clock.stop=True
    with pytest.raises(meteo.Cancelled):gate.acquire(clock,stop)
    assert gate.active==0

def test_calculation_downloads_only_needed_and_reuses_cache(tmp_path):
    calls=[];a=b=date(2023,1,1)
    def fake(url,cancel,on_retry):calls.append(url);return raw(a,b)
    args=(plan(),a,b,50,[Model(1),Model(2),Model(3)],tmp_path/'cache')
    result=run(*args,tmp_path/'out',Event(),lambda *a:None,1,fake)
    assert result['status']=='complete' and len(calls)==1
    assert 'start=20230101' in calls[0] and 'end=20230101' in calls[0]
    result=run(*args,tmp_path/'out2',Event(),lambda *a:None,1,fake)
    assert result['status']=='complete' and len(calls)==1

def test_unavailable_hours_never_produce_final_samples(tmp_path):
    a=b=date(2023,1,1)
    def fake(*args,**kw):return raw(a,b,'2023010123')
    result=run(plan(),a,b,50,[Model(1)],tmp_path/'cache',tmp_path/'out',Event(),lambda *a:None,1,fake)
    assert result['status']=='incomplete' and not result['cells'] and 'means' not in result
