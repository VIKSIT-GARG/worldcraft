"""Real Google SDK serialization with mocked HTTP/ADC; NOT live provider evidence."""
import json

import google.auth
import httpx
import pytest
from google.auth.exceptions import DefaultCredentialsError, RefreshError
from google.oauth2.credentials import Credentials

from backend import integrations


@pytest.fixture
def vertex(monkeypatch):
    for key, value in [('GOOGLE_CLOUD_PROJECT', 'test-project'),
                       ('GOOGLE_CLOUD_LOCATION', 'global'),
                       ('GEMINI_MODEL_ID', 'gemini-3.8-flash')]:
        monkeypatch.setenv(key, value)
    # Ambient Developer API configuration must never redirect the planner.
    monkeypatch.setenv('GOOGLE_API_KEY', 'test-only-developer-key')
    monkeypatch.setenv('GOOGLE_GENAI_USE_VERTEXAI', 'false')
    monkeypatch.setattr(google.auth, 'default', lambda **kw: (Credentials(token='mock-token'), 'adc-project'))
    state = {'requests': [], 'status': 200, 'operation': {'action': 'restrict', 'road': 'main'},
             'error': None, 'finish': 'STOP', 'exception': None}

    def send(client, request, **kwargs):
        state['requests'].append(request)
        if state['exception']:
            raise state['exception']
        if state['status'] != 200:
            body = {'error': {'code': state['status'], 'message': 'secret-provider-body'}}
        else:
            text = state.get('raw', json.dumps({'operation': state['operation'], 'error': state['error']}))
            body = {'responseId': 'mock-response-id', 'modelVersion': 'mock-version',
                    'candidates': [{'content': {'role': 'model', 'parts': [{'text': text}]},
                                    'finishReason': state['finish']}]}
        return httpx.Response(state['status'], json=body, request=request)

    monkeypatch.setattr(httpx.Client, 'send', send)
    return state


def test_vertex_sdk_request_and_context(vertex):
    context = {'scenario_id': 'world-1', 'worlds': [{'id': 'world-1', 'blocked': []}]}
    operation, response_id = integrations.gemini_plan('main road mat lena', context)
    assert operation.action == 'restrict' and operation.road == 'main'
    assert response_id == 'mock-response-id'
    assert len(vertex['requests']) == 1
    request = vertex['requests'][0]
    assert str(request.url) == ('https://aiplatform.googleapis.com/v1/projects/test-project/'
                                'locations/global/publishers/google/models/gemini-3.8-flash:generateContent')
    assert 'x-goog-api-key' not in request.headers
    assert 0 < request.extensions['timeout']['read'] <= 45
    body = json.loads(request.content)
    assert json.loads(body['contents'][0]['parts'][0]['text']) == {
        'instruction': 'main road mat lena', 'context': context}
    assert 'Hindi' in body['systemInstruction']['parts'][0]['text']
    config = body['generationConfig']
    assert config['responseMimeType'] == 'application/json'
    schema = config['responseSchema']
    assert set(schema['required']) == {'operation', 'error'}
    props = schema['properties']['operation']['properties']
    assert props['action']['enum'] == ['create_city', 'dispatch', 'restrict', 'unrestrict', 'clone', 'run', 'compare', 'break_plan', 'set_deadline', 'explain', 'cyberwave_run', 'robot_prepare', 'robot_sync', 'robot_execute', 'robot_stop', 'robot_create', 'robot_forward', 'robot_observe', 'robot_catalog', 'robot_design']
    assert props['ambulances']['maximum'] == 4
    # The full local model is stricter than Vertex's supported schema subset.
    assert 'additionalProperties' not in json.dumps(schema)
    assert 'maxLength' not in json.dumps(schema)
    assert 'default' not in json.dumps(schema)


@pytest.mark.parametrize('action', ['create_city', 'dispatch', 'restrict', 'unrestrict', 'clone', 'run', 'compare'])
def test_supported_operations(vertex, action):
    vertex['operation'] = {'action': action}
    if action=='dispatch':vertex['operation']['destination']='A'
    operation, _ = integrations.gemini_plan('instruction', {})
    assert operation.action == action


def test_dispatch_requires_explicit_destination_in_provider_schema(vertex):
    schema=integrations.gemini_response_schema()['properties']['operation']
    assert 'destination' in schema['required']
    assert 'dispatch' in schema['properties']['destination']['description'].lower()
    vertex['operation']={'action':'dispatch','name':'C'}
    with pytest.raises(integrations.IntegrationError) as exc:
        integrations.gemini_plan('Send an ambulance to Tower C.',{})
    assert exc.value.status==502


@pytest.mark.parametrize('destination',['B','C','E'])
def test_dispatch_preserves_explicit_target(vertex,destination):
    vertex['operation']={'action':'dispatch','destination':destination}
    operation,_=integrations.gemini_plan(f'Send an ambulance to Tower {destination}.',{})
    assert operation.destination==destination


@pytest.mark.parametrize('operation', [
    {'action': 'exec', 'code': 'arbitrary code'},
    {'action': 'restrict', 'shell': 'arbitrary code'},
    {'action': 'create_city', 'ambulances': 999},
    {'action': 'dispatch', 'destination': 'x' * 31},
    {'action': 'create_city', 'buildings': []},
])
def test_reject_invalid_operations(vertex, operation):
    vertex['operation'] = operation
    with pytest.raises(integrations.IntegrationError) as exc:
        integrations.gemini_plan('instruction', {})
    assert exc.value.provider == 'gemini' and exc.value.status == 502


@pytest.mark.parametrize('raw', ['', '{}', 'null', '[]', 'not JSON',
                                  '{"operation":null,"error":null}',
                                  '{"operation":{"action":"run"},"error":"ambiguous"}',
                                  '{"operation":{"action":"run"},"error":null,"extra":true}'])
def test_reject_malformed_or_inconsistent_output(vertex, raw):
    vertex['raw'] = raw
    with pytest.raises(integrations.IntegrationError) as exc:
        integrations.gemini_plan('instruction', {})
    assert exc.value.status == 502


@pytest.mark.parametrize('finish', ['MAX_TOKENS', 'SAFETY', 'OTHER'])
def test_reject_unfinished_or_blocked_response(vertex, finish):
    vertex['finish'] = finish
    with pytest.raises(integrations.IntegrationError) as exc:
        integrations.gemini_plan('instruction', {})
    assert exc.value.status == 502


def test_structured_refusal_is_not_an_operation(vertex):
    vertex.update(operation=None, error='Specify a supported city operation.')
    with pytest.raises(integrations.IntegrationError) as exc:
        integrations.gemini_plan('delete everything', {})
    assert exc.value.status == 422


@pytest.mark.parametrize('code, expected', [(400, 502), (401, 503), (403, 503), (404, 503),
                                          (429, 429), (500, 502), (503, 502), (504, 504)])
def test_upstream_failures_are_sanitized_and_not_retried(vertex, code, expected):
    vertex['status'] = code
    with pytest.raises(integrations.IntegrationError) as exc:
        integrations.gemini_plan('instruction', {})
    assert exc.value.status == expected and exc.value.provider == 'gemini'
    assert 'secret-provider-body' not in exc.value.message
    assert len(vertex['requests']) == 1


@pytest.mark.parametrize('exception, expected', [(httpx.ReadTimeout('secret-url'), 504),
                                               (httpx.ConnectError('secret-url'), 502),
                                               (RefreshError('secret-token'), 503)])
def test_network_and_auth_failures(vertex, exception, expected):
    vertex['exception'] = exception
    with pytest.raises(integrations.IntegrationError) as exc:
        integrations.gemini_plan('instruction', {})
    assert exc.value.status == expected and 'secret-' not in exc.value.message


@pytest.mark.parametrize('missing', ['GOOGLE_CLOUD_PROJECT', 'GOOGLE_CLOUD_LOCATION', 'GEMINI_MODEL_ID'])
def test_missing_configuration_is_blocked_before_network(vertex, monkeypatch, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(integrations.IntegrationError) as exc:
        integrations.gemini_plan('instruction', {})
    assert exc.value.status == 503 and missing in exc.value.message
    assert vertex['requests'] == []
    assert not integrations.status()['gemini']['configured']


def test_missing_adc_is_blocked_not_api_key_fallback(vertex, monkeypatch):
    def no_adc(**kwargs):
        raise DefaultCredentialsError('secret-credential-path')
    monkeypatch.setattr(google.auth, 'default', no_adc)
    with pytest.raises(integrations.IntegrationError) as exc:
        integrations.gemini_plan('instruction', {})
    assert exc.value.status == 503 and 'ADC' in exc.value.message
    assert 'secret-' not in exc.value.message and vertex['requests'] == []


def test_configuration_is_not_verification(vertex):
    status = integrations.status()['gemini']
    assert status['configured'] and not status['verified']


def test_city_planner_does_not_duplicate_the_reserved_hospital(vertex):
    vertex['operation']={'action':'create_city','buildings':['hospital','A','B','C','D'],'ambulances':2}
    operation,_=integrations.gemini_plan('चार इमारतें और एक अस्पताल',{})
    assert operation.buildings==['A','B','C','D']


def test_configured_location_and_model_are_used(vertex, monkeypatch):
    monkeypatch.setenv('GOOGLE_CLOUD_LOCATION', 'eu')
    integrations.gemini_plan('instruction', {})
    request = vertex['requests'][0]
    assert request.url.host == 'aiplatform.eu.rep.googleapis.com'
    assert '/locations/eu/publishers/google/models/gemini-3.8-flash:generateContent' in request.url.path
