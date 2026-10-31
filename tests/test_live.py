"""Opt-in LIVE requests; skipped without credentials. Never mock these tests."""
import asyncio
import os
from pathlib import Path
import pytest
from backend import integrations

pytestmark=pytest.mark.live

def enabled(*names):
    if os.getenv('RUN_LIVE_INTEGRATIONS')!='1':pytest.skip('Set RUN_LIVE_INTEGRATIONS=1 for real network calls.')
    if not all(os.getenv(x) for x in names):pytest.skip('Missing live configuration: '+', '.join(names))

def test_live_gemini():
    enabled(*integrations.GEMINI_CONFIG)
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError
    try:
        google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    except DefaultCredentialsError:
        pytest.skip('Vertex AI ADC unavailable; configure gcloud application-default login or workload ADC.')
    op,request_id=integrations.gemini_plan('Ek city banao: chaar buildings A, B, C, D, ek hospital aur do ambulances.',{})
    assert op.action=='create_city' and op.buildings==['A','B','C','D'] and op.ambulances==2
    assert request_id

def test_live_sarvam():
    enabled('SARVAM_API_KEY','SARVAM_TEST_AUDIO')
    p=Path(os.environ['SARVAM_TEST_AUDIO'])
    r=asyncio.run(integrations.transcribe(p.read_bytes(),p.name,'application/octet-stream'))
    assert r['transcript'].strip() and r['model']=='saaras:v4'

def test_live_cyberwave():
    enabled('CYBERWAVE_API_KEY','CYBERWAVE_TWIN_ID')
    result=asyncio.run(integrations.cyberwave_probe())
    assert result['authenticated'] and result['command_sent'] and result['state_observed'] and result['motion_observed'] and result['verified'],result
