"""Adapter contract tests use mocks and are NOT evidence of live provider success."""
import asyncio
import time
import pytest
from backend import integrations
from backend.cyberwave_probe import read_sample

def test_telemetry_requires_fresh_simulator_source():
    now=time.time()
    good={'source_type':'sim','timestamp':now,'position':{'x':1,'y':2,'z':3}}
    assert read_sample(good,now)['position']==[1,2,3]
    for bad in [{**good,'source_type':'edit'},{**good,'source_type':'sim_tele'},{**good,'source_type':'edge'},{**good,'timestamp':now-100},{**good,'timestamp':float('nan')},{**good,'position':{'x':float('nan'),'y':0,'z':0}}]:
        assert read_sample(bad,now) is None

def test_installed_cyberwave_contract():
    import inspect
    from cyberwave import Cyberwave
    from cyberwave.twin import LocomoteTwin, Twin
    assert 'mode' in inspect.signature(Cyberwave).parameters
    assert 'twin_id' in inspect.signature(Cyberwave.twin).parameters
    assert 'source_type' in inspect.signature(LocomoteTwin.move_forward).parameters
    assert callable(Twin.subscribe_position)

def test_sarvam_http_contract(monkeypatch):
    monkeypatch.setenv('SARVAM_API_KEY','test-only-not-a-real-key')
    calls=[]
    class Response:
        status_code=200
        def json(self):return {'transcript':'एक शहर बनाओ','language_code':'hi-IN','request_id':'mock-request'}
    class Client:
        def __init__(self,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def post(self,url,**kw):calls.append((url,kw));return Response()
    monkeypatch.setattr(integrations.httpx,'AsyncClient',Client)
    result=asyncio.run(integrations.transcribe(b'fixture','clip.webm','audio/webm'))
    assert result['transcript']=='एक शहर बनाओ'
    url,kw=calls[0]
    assert url=='https://api.sarvam.ai/speech-to-text'
    assert kw['data']['model']=='saaras:v4'
    assert kw['headers']['api-subscription-key']=='test-only-not-a-real-key'
    assert 'file' in kw['files']
