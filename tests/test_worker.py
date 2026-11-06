"""Worker HTTP contracts: test transport only, no real model or listening sockets."""
import asyncio
import json
import sys
import time

import httpx
import pytest

from worker.client import LeaseLost, Worker, main, read_limits_override

JOB = {'id': 'synthetic-job', 'model': 'demo-model', 'privacy': 'trusted',
       'lease_token': 'synthetic-lease', 'messages': [{'role': 'user', 'content': 'test'}],
       'max_output_tokens': 128}


def transport_for(monkeypatch, handler):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr('worker.client.httpx.AsyncClient',
                        lambda *args, **kwargs: real_client(*args, **{**kwargs, 'transport': transport}))
    return transport


def run_job(worker, job=JOB):
    async def run():
        async with httpx.AsyncClient() as client:
            await worker.run_job(client, job)
    asyncio.run(run())


@pytest.mark.parametrize('requested,expected', [(128, 32), (8, 8)])
def test_ollama_uses_only_local_options_and_caps_output(monkeypatch, requested, expected):
    calls = []
    def handler(request):
        data = json.loads(request.content)
        calls.append((request, data))
        if request.url.path == '/api/chat':
            assert 'Authorization' not in request.headers  # coordinator credential stays separate
            assert data == {'model': 'demo-model', 'messages': JOB['messages'], 'stream': False,
                            'keep_alive': 0, 'options': {
                                'num_ctx': 1024, 'num_thread': 2, 'num_predict': expected,
                                'num_gpu': 0, 'num_batch': 32}}
            return httpx.Response(200, json={'done': True, 'message': {'content': 'native reply'},
                                            'prompt_eval_count': 11, 'eval_count': 3})
        assert request.url.path == '/api/worker/complete'
        assert request.headers['Authorization'] == 'Bearer synthetic-worker-token'
        assert data['ok'] is True and data['content'] == 'native reply'
        assert data['usage'] == {'prompt_tokens': 11, 'completion_tokens': 3, 'total_tokens': 14}
        return httpx.Response(200, json={'accepted': True})
    transport_for(monkeypatch, handler)
    w = Worker('http://coordinator', 'synthetic-worker-token', 'demo-model',
               'http://127.0.0.1:11434/api/chat', backend='ollama')
    run_job(w, {**JOB, 'max_output_tokens': requested,
                'options': {'num_gpu': 99, 'num_thread': 999}, 'llm_url': 'http://untrusted'})
    assert len(calls) == 2


def test_openai_backend_remains_compatible(monkeypatch):
    paths = []
    def handler(request):
        data = json.loads(request.content)
        paths.append(request.url.path)
        if request.url.path == '/v1/chat/completions':
            assert data == {'model': 'demo-model', 'messages': JOB['messages'],
                            'max_tokens': 128, 'stream': False}
            assert request.headers['Authorization'] == 'Bearer synthetic-model-key'
            return httpx.Response(200, json={'choices': [{'message': {'content': 'openai reply'}}],
                                            'usage': {'total_tokens': 9}})
        assert data['content'] == 'openai reply' and data['usage'] == {'total_tokens': 9}
        assert request.headers['Authorization'] == 'Bearer synthetic-worker-token'
        return httpx.Response(200, json={'accepted': True})
    transport_for(monkeypatch, handler)
    run_job(Worker('http://coordinator', 'synthetic-worker-token', 'demo-model',
                   'http://127.0.0.1:8080/v1/chat/completions', llm_api_key='synthetic-model-key'))
    assert paths == ['/v1/chat/completions', '/api/worker/complete']


@pytest.mark.parametrize('options', [
    {'backend': 'unknown'}, {'context_tokens': 0}, {'context_tokens': 8193},
    {'cpu_threads': 0}, {'cpu_threads': 9},
    {'max_output_tokens': 0}, {'max_output_tokens': 513},
])
def test_invalid_local_settings_fail_before_network(options):
    with pytest.raises(ValueError):
        Worker('http://coordinator', 'synthetic-token', 'demo-model',
               'http://127.0.0.1:11434/api/chat', **{'backend': 'ollama', **options})


@pytest.mark.parametrize('url', ['http://remote.example/api/chat', 'https://127.0.0.1/api/chat',
                               'http://127.0.0.1@remote.example/api/chat',
                               'http://127.0.0.1:11434/api/chat?secret=value'])
def test_ollama_rejects_non_loopback_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        Worker('http://coordinator', 'synthetic-token', 'demo-model', url, backend='ollama')


def test_model_mismatch_never_reaches_backend(monkeypatch):
    calls = []
    transport_for(monkeypatch, lambda request: calls.append(request))
    w = Worker('http://coordinator', 'synthetic-token', 'approved-model',
               'http://127.0.0.1:11434/api/chat', backend='ollama')
    with pytest.raises(ValueError, match='model'):
        run_job(w)
    assert calls == []


@pytest.mark.parametrize('requested', [-1, 0, True, '32'])
def test_invalid_job_token_budget_cannot_enable_unlimited_generation(monkeypatch, requested):
    calls = []
    transport_for(monkeypatch, lambda request: calls.append(request))
    w = Worker('http://coordinator', 'synthetic-token', 'demo-model',
               'http://127.0.0.1:11434/api/chat', backend='ollama')
    with pytest.raises(ValueError, match='positive integer'):
        run_job(w, {**JOB, 'max_output_tokens': requested})
    assert calls == []


@pytest.mark.parametrize('output', [
    {'done': False, 'message': {'content': 'partial'}},
    {'message': {'content': 'missing completion flag'}},
    {'done': True, 'message': {'content': '  '}},
    {'done': True, 'message': {'content': None}},
])
def test_invalid_ollama_response_is_not_submitted_as_success(monkeypatch, output):
    paths = []
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json=output)
    transport_for(monkeypatch, handler)
    w = Worker('http://coordinator', 'synthetic-token', 'demo-model',
               'http://127.0.0.1:11434/api/chat', backend='ollama')
    with pytest.raises(ValueError):
        run_job(w)
    assert paths == ['/api/chat']


def test_cli_reads_ollama_local_configuration(monkeypatch):
    for name in ['CAG_LLM_URL', 'CAG_LLM_API_KEY']:
        monkeypatch.delenv(name, raising=False)
    for name, value in {'CAG_LLM_BACKEND': 'ollama', 'CAG_COORD_URL': 'http://127.0.0.1:8765',
                        'CAG_WORKER_TOKEN': 'cag_synthetic', 'CAG_MODEL': 'demo-model',
                        'CAG_OLLAMA_NUM_CTX': '512', 'CAG_OLLAMA_NUM_THREAD': '1',
                        'CAG_OLLAMA_MAX_OUTPUT_TOKENS': '16'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, 'argv', ['worker.client', '--once'])
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path == '/api/worker/claim':
            return httpx.Response(200, json={'job': JOB})
        if request.url.path == '/api/chat':
            data = json.loads(request.content)
            assert request.url.port == 11434
            assert data['options']['num_ctx'] == 512
            assert data['options']['num_thread'] == 1
            assert data['options']['num_predict'] == 16
            return httpx.Response(200, json={'done': True, 'message': {'content': 'reply'}})
        return httpx.Response(200, json={'ok': True})
    transport_for(monkeypatch, handler)
    main()
    assert '/api/chat' in calls
    assert '/api/worker/complete' in calls


@pytest.mark.parametrize('report_status', [200, 409])
def test_once_reports_inference_failure_and_exits_with_error(monkeypatch, report_status, capsys):
    failures = []
    def handler(request):
        if request.url.path == '/api/worker/claim':
            return httpx.Response(200, json={'job': JOB})
        if request.url.path == '/v1/chat/completions':
            return httpx.Response(503, text='unavailable')
        if request.url.path == '/api/worker/complete':
            failures.append(json.loads(request.content))
            return httpx.Response(report_status, json={'detail': 'test response'})
        return httpx.Response(200, json={'ok': True})
    transport_for(monkeypatch, handler)
    w = Worker('http://coordinator', 'synthetic-token', 'demo-model',
               'http://127.0.0.1:8080/v1/chat/completions')
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(w.run(once=True))
    assert len(failures) == 1 and failures[0]['ok'] is False
    if report_status == 409:
        assert 'Could not report failure' in capsys.readouterr().err


@pytest.mark.parametrize('lost_status', [409, 403])
def test_lease_loss_aborts_inference_without_completion(monkeypatch, lost_status):
    posted = []
    async def handler(request):
        path = request.url.path
        if path == '/api/worker/claim':
            return httpx.Response(200, json={'job': JOB})
        if path == '/api/worker/heartbeat':
            has_job = 'job_id' in json.loads(request.content)
            return httpx.Response(lost_status if has_job else 200, json={'detail': 'synthetic'})
        if path == '/v1/chat/completions':
            await asyncio.sleep(10)
            return httpx.Response(200, json={'choices': [{'message': {'content': 'too late'}}]})
        if path == '/api/worker/me':  # harmless self-status poll, never a completion report
            return httpx.Response(200, json={'provisional_credits': 0})
        posted.append(path)
        return httpx.Response(200, json={'accepted': True})
    transport_for(monkeypatch, handler)
    w = Worker('http://coordinator', 'synthetic-token', 'demo-model',
               'http://127.0.0.1:8080/v1/chat/completions', lease_heartbeat_seconds=0.05)
    started = time.monotonic()
    with pytest.raises(LeaseLost):
        asyncio.run(w.run(once=True))
    assert time.monotonic() - started < 3
    assert posted == []  # in particular, no completion report for the aborted job


def test_transient_heartbeat_error_does_not_abort(monkeypatch):
    completions, codes = [], iter([500, 500])
    async def handler(request):
        path = request.url.path
        if path == '/api/worker/claim':
            return httpx.Response(200, json={'job': JOB})
        if path == '/api/worker/heartbeat':
            has_job = 'job_id' in json.loads(request.content)
            return httpx.Response(next(codes, 200) if has_job else 200, json={})
        if path == '/v1/chat/completions':
            await asyncio.sleep(0.3)
            return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})
        completions.append(json.loads(request.content))
        return httpx.Response(200, json={'accepted': True})
    transport_for(monkeypatch, handler)
    w = Worker('http://coordinator', 'synthetic-token', 'demo-model',
               'http://127.0.0.1:8080/v1/chat/completions', lease_heartbeat_seconds=0.05)
    asyncio.run(w.run(once=True))
    assert len(completions) == 1 and completions[0]['ok'] is True


@pytest.mark.parametrize('seconds', [0, -1])
def test_non_positive_heartbeat_interval_is_rejected(seconds):
    with pytest.raises(ValueError):
        Worker('http://c', 't', 'demo-model', 'http://127.0.0.1:8080/v1/chat/completions',
               lease_heartbeat_seconds=seconds)


def test_hardware_metadata_has_no_hostname():
    from worker.client import hardware
    assert 'hostname' not in hardware()


def test_ollama_gpu_layers_are_local_operator_setting(monkeypatch):
    seen = []
    def handler(request):
        if request.url.path == '/api/chat':
            seen.append(json.loads(request.content)['options']['num_gpu'])
            return httpx.Response(200, json={'done': True, 'message': {'content': 'x'}})
        return httpx.Response(200, json={'accepted': True})
    transport_for(monkeypatch, handler)
    w = Worker('http://coordinator', 't', 'demo-model', 'http://127.0.0.1:11434/api/chat',
               backend='ollama', gpu_layers=99)
    run_job(w, {**JOB, 'options': {'num_gpu': 0}})
    assert seen == [99]


@pytest.mark.parametrize('layers', [-1, 1000, 1.5])
def test_invalid_gpu_layers_rejected(layers):
    with pytest.raises(ValueError, match='CAG_OLLAMA_NUM_GPU'):
        Worker('http://c', 't', 'demo-model', 'http://127.0.0.1:11434/api/chat',
               backend='ollama', gpu_layers=layers)


def test_lease_cleared_by_own_completion_does_not_abort_report(monkeypatch):
    """The completion clears the lease; a heartbeat racing the slow response sees 409."""
    completions = []
    async def handler(request):
        path = request.url.path
        if path == '/api/worker/claim':
            return httpx.Response(200, json={'job': JOB})
        if path == '/api/worker/heartbeat':
            has_job = 'job_id' in json.loads(request.content)
            return httpx.Response(409 if has_job and completions else 200, json={})
        if path == '/v1/chat/completions':
            return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})
        completions.append(json.loads(request.content))
        await asyncio.sleep(0.3)  # committed on the coordinator, response still in flight
        return httpx.Response(200, json={'accepted': True})
    transport_for(monkeypatch, handler)
    w = Worker('http://coordinator', 'synthetic-token', 'demo-model',
               'http://127.0.0.1:8080/v1/chat/completions', lease_heartbeat_seconds=0.05)
    asyncio.run(w.run(once=True))
    assert len(completions) == 1 and completions[0]['ok'] is True


def test_write_status_is_a_noop_without_a_configured_path(tmp_path):
    w = Worker('http://coordinator', 'synthetic-token', 'demo-model',
               'http://127.0.0.1:8080/v1/chat/completions')
    w.write_status(connected=True)
    assert list(tmp_path.iterdir()) == []


def test_write_status_merges_fields_across_calls(tmp_path):
    status = tmp_path / 'nested' / 'status.json'
    w = Worker('http://coordinator', 'synthetic-token', 'demo-model',
               'http://127.0.0.1:8080/v1/chat/completions', status_path=str(status))
    w.write_status(connected=True, last_heartbeat='t1')
    w.write_status(job_status='running', job_id='job-1')
    data = json.loads(status.read_text())
    assert data == {'model': 'demo-model', 'backend': 'openai', 'connected': True,
                    'last_heartbeat': 't1', 'job_status': 'running', 'job_id': 'job-1'}
    assert not status.with_suffix('.json.tmp').exists()


def test_run_once_writes_completion_status(monkeypatch, tmp_path):
    status = tmp_path / 'status.json'
    def handler(request):
        if request.url.path == '/api/worker/claim':
            return httpx.Response(200, json={'job': JOB})
        if request.url.path == '/v1/chat/completions':
            return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})
        return httpx.Response(200, json={'accepted': True})
    transport_for(monkeypatch, handler)
    w = Worker('http://coordinator', 'synthetic-token', 'demo-model',
               'http://127.0.0.1:8080/v1/chat/completions', status_path=str(status))
    asyncio.run(w.run(once=True))
    data = json.loads(status.read_text())
    assert data['job_status'] == 'idle' and data['last_job_id'] == JOB['id'] and data['last_error'] is None
    assert 'last_success' in data


def test_jobs_completed_counter_increments_across_runs(monkeypatch, tmp_path):
    status = tmp_path / 'status.json'
    def handler(request):
        if request.url.path == '/api/worker/claim':
            return httpx.Response(200, json={'job': JOB})
        if request.url.path == '/v1/chat/completions':
            return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})
        return httpx.Response(200, json={'accepted': True})
    transport_for(monkeypatch, handler)
    w = Worker('http://coordinator', 'synthetic-token', 'demo-model',
               'http://127.0.0.1:8080/v1/chat/completions', status_path=str(status))
    asyncio.run(w.run(once=True))
    assert json.loads(status.read_text())['jobs_completed'] == 1
    asyncio.run(w.run(once=True))
    assert json.loads(status.read_text())['jobs_completed'] == 2


def test_fetch_self_status_writes_provisional_credits(monkeypatch, tmp_path):
    status = tmp_path / 'status.json'
    def handler(request):
        if request.url.path == '/api/worker/me':
            return httpx.Response(200, json={'provisional_credits': 15, 'trust_level': 'community'})
        if request.url.path == '/api/worker/claim':
            return httpx.Response(200, json={'job': None})
        return httpx.Response(200, json={'ok': True})
    transport_for(monkeypatch, handler)
    w = Worker('http://coordinator', 'synthetic-token', 'demo-model',
               'http://127.0.0.1:8080/v1/chat/completions', status_path=str(status))
    asyncio.run(w.run(once=True))
    data = json.loads(status.read_text())
    assert data['provisional_credits'] == 15 and data['trust_level'] == 'community'


@pytest.mark.parametrize('data,expected', [
    ({'cpu_threads': 4, 'context_tokens': 2048}, {'cpu_threads': 4, 'context_tokens': 2048}),
    ({'cpu_threads': 'not-an-int'}, {}),
    ({'unknown_field': 9}, {}),
    ({}, {}),
])
def test_read_limits_override_filters_unknown_and_malformed(tmp_path, data, expected):
    path = tmp_path / 'limits.json'
    path.write_text(json.dumps(data))
    assert read_limits_override(str(path)) == expected


def test_read_limits_override_missing_or_empty_path_is_noop(tmp_path):
    assert read_limits_override('') == {}
    assert read_limits_override(str(tmp_path / 'missing.json')) == {}


def test_cli_applies_local_limits_override(monkeypatch, tmp_path):
    limits_path = tmp_path / 'limits.json'
    limits_path.write_text(json.dumps({'cpu_threads': 7}))
    for name in ['CAG_LLM_URL', 'CAG_LLM_API_KEY']:
        monkeypatch.delenv(name, raising=False)
    for name, value in {'CAG_LLM_BACKEND': 'ollama', 'CAG_COORD_URL': 'http://127.0.0.1:8765',
                        'CAG_WORKER_TOKEN': 'cag_synthetic', 'CAG_MODEL': 'demo-model',
                        'CAG_OLLAMA_NUM_THREAD': '1', 'CAG_LIMITS_PATH': str(limits_path)}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, 'argv', ['worker.client', '--once'])
    def handler(request):
        if request.url.path == '/api/worker/claim':
            return httpx.Response(200, json={'job': JOB})
        if request.url.path == '/api/chat':
            data = json.loads(request.content)
            assert data['options']['num_thread'] == 7  # overridden, not the env default of 1
            return httpx.Response(200, json={'done': True, 'message': {'content': 'reply'}})
        return httpx.Response(200, json={'ok': True})
    transport_for(monkeypatch, handler)
    main()
