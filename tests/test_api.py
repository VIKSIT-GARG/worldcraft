import pytest
from fastapi.testclient import TestClient
from backend import main
from backend.engine import Engine
from backend import integrations
from backend.engine import Operation

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main,'engine',Engine())
    monkeypatch.delenv('SARVAM_API_KEY',raising=False)
    for key in integrations.GEMINI_CONFIG:
        monkeypatch.delenv(key,raising=False)
    monkeypatch.delenv('CYBERWAVE_API_KEY',raising=False)
    monkeypatch.delenv('CYBERWAVE_TWIN_ID',raising=False)
    monkeypatch.setattr(main,'provider_status',integrations.status())
    main.control.update(paused=True,speed=1)
    with TestClient(main.app) as c:yield c

def test_mandatory_demo_via_api(client):
    r=client.post('/api/command',json={'text':'Create a city with four buildings A, B, C, D, one hospital and two ambulances.','mode':'local'})
    assert r.status_code==200;sid=r.json()['scenario_id']
    def cmd(text):return client.post('/api/command',json={'text':text,'mode':'local','scenario_id':sid})
    assert cmd('Send an ambulance from the hospital to Tower A.').status_code==200
    assert cmd("Don't use the main road.").status_code==200
    r=cmd('Create a parallel scenario');other=r.json()['scenario_id']
    r=client.post('/api/command',json={'text':'Allow main road','mode':'local','scenario_id':other});assert r.status_code==200
    assert cmd('Compare scenarios').status_code==200
    main.engine.tick(90)
    state=client.get('/api/state').json()
    m=[s['ambulances'][0]['mission'] for s in state['scenarios']]
    assert [x['distance'] for x in m]==[720,480]
    assert [x['elapsed'] for x in m]==[90,40]
    assert all(x['success'] for x in m)

def test_missing_credentials_not_success(client):
    r=client.post('/api/command',json={'text':'Create city','mode':'gemini'})
    assert r.status_code==503 and r.json()['provider']=='gemini'
    assert client.get('/api/state').json()['scenarios']==[]
    r=client.post('/api/transcribe',files={'file':('audio.wav',b'not real audio','audio/wav')})
    assert r.status_code==503 and r.json()['provider']=='sarvam'
    r=client.post('/api/integrations/cyberwave/probe',json={})
    assert r.status_code==503 and r.json()['provider']=='cyberwave'

def test_local_does_not_pretend_general_ai(client):
    r=client.post('/api/command',json={'text':'ek sheher banao','mode':'local'})
    assert r.status_code==422
    assert 'Gemini' in r.json()['detail']

def test_invalid_operations_and_audio(client):
    assert client.post('/api/action',json={'operation':{'action':'dispatch'},'scenario_id':'missing'}).status_code==422
    assert client.post('/api/action',json={'operation':{'action':'shell'}}).status_code==422
    assert client.post('/api/control',json={'speed':100}).status_code==422
    assert client.post('/api/transcribe',files={'file':('empty.wav',b'','audio/wav')}).status_code==422

def test_health(client): assert client.get('/api/health').json()['ok']


def test_gemini_is_default_and_provider_evidence_starts_unverified(client):
    state=client.get('/api/state').json()
    assert set(state['integrations'])=={'gemini','sarvam','cyberwave'}
    assert not state['integrations']['gemini']['configured']
    assert not state['integrations']['gemini']['verified']
    r=client.post('/api/command',json={'text':'Create city'})
    assert r.status_code==503 and r.json()['provider']=='gemini'
    assert client.get('/api/state').json()['scenarios']==[]


def test_gemini_success_and_failure_evidence_with_world_context(client,monkeypatch):
    # This test verifies status transitions with a mocked planner, NOT live success.
    sid=client.post('/api/command',json={'text':'Create city','mode':'local'}).json()['scenario_id']
    client.post('/api/action',json={'operation':{'action':'dispatch'},'scenario_id':sid})
    received=[]
    def plan(text,context):
        received.append(context)
        return Operation(action='restrict'), 'mock-response-id'
    monkeypatch.setattr(integrations,'gemini_plan',plan)
    r=client.post('/api/command',json={'text':'main road mat lena','mode':'gemini','scenario_id':sid})
    assert r.status_code==200 and r.json()['planner']=='gemini'
    assert r.json()['request_id']=='mock-response-id'
    assert r.json()['operation']['action']=='restrict'
    assert received[0]['scenario_id']==sid
    world=received[0]['worlds'][0]
    assert world['id']==sid and world['buildings']==['hospital','A','B','C','D']
    assert world['ambulances'][0]['mission']['destination']=='A'
    assert len(world['roads'])>0
    status=client.get('/api/state').json()['integrations']['gemini']
    assert status['verified'] and status['request_id']=='mock-response-id'
    before=main.engine.snapshot()
    def fail(*args):
        raise integrations.IntegrationError('gemini','Quota limit reached.',429)
    monkeypatch.setattr(integrations,'gemini_plan',fail)
    # Local parser could accept this; Gemini failure must never fall back to it.
    r=client.post('/api/command',json={'text':'Create city','mode':'gemini','scenario_id':sid})
    assert r.status_code==429 and main.engine.snapshot()==before
    assert not client.get('/api/state').json()['integrations']['gemini']['verified']


def test_legacy_planner_mode_rejected(client):
    r=client.post('/api/command',json={'text':'Create city','mode':'bedrock'})
    assert r.status_code==422 and main.engine.snapshot()['scenarios']==[]
