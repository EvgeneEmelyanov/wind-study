from http.client import IncompleteRead
from urllib.error import HTTPError
import pytest
from windstudy import meteo

class Cancel:
    def __init__(self):self.waits=[];self.stop=False
    def is_set(self):return self.stop
    def wait(self,delay):self.waits.append(delay);return self.stop

class Response:
    def __init__(self,error=None):self.error=error
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self):
        if self.error:raise self.error
        return b'complete response'

@pytest.mark.parametrize('error',[ConnectionResetError(10054,'reset'),IncompleteRead(b'partial',10)])
def test_read_failure_retried(monkeypatch,error):
    responses=iter([Response(error),Response()])
    monkeypatch.setattr(meteo,'urlopen',lambda *args,**kwargs:next(responses))
    cancel=Cancel();messages=[]
    assert meteo.fetch('url',cancel,messages.append)==b'complete response'
    assert cancel.waits==[5] and len(messages)==1

def test_exhaustion_and_cancel(monkeypatch):
    monkeypatch.setattr(meteo,'urlopen',lambda *args,**kwargs:Response(ConnectionResetError('reset')))
    cancel=Cancel()
    with pytest.raises(ConnectionError,match='6 попыток'):meteo.fetch('url',cancel)
    assert cancel.waits==[5,10,20,40,60]
    cancel=Cancel()
    def stop(message):cancel.stop=True
    with pytest.raises(meteo.Cancelled):meteo.fetch('url',cancel,stop)

def test_http_status_and_retry_after(monkeypatch):
    calls=[0]
    def request(*args,**kwargs):
        calls[0]+=1
        if calls[0]==1:raise HTTPError('url',429,'limit',{'Retry-After':'90'},None)
        return Response()
    monkeypatch.setattr(meteo,'urlopen',request);cancel=Cancel()
    assert meteo.fetch('url',cancel)==b'complete response' and cancel.waits==[90]
    def denied(*args,**kwargs):raise HTTPError('url',400,'bad request',{},None)
    monkeypatch.setattr(meteo,'urlopen',denied);cancel=Cancel()
    with pytest.raises(HTTPError):meteo.fetch('url',cancel)
    assert not cancel.waits
