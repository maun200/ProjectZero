"""Explicit, loopback-only real-model integration test; never downloads weights.

Run only with an operator-approved, already installed model:
    python scripts/smoke_test_ollama.py --model qwen2.5-coder:7b-instruct
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx

from ollama_preflight import check_backend

ROOT = Path(__file__).resolve().parent.parent
OLLAMA = 'http://127.0.0.1:11434'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True, help='Exact locally installed, operator-approved model tag')
    parser.add_argument('--min-free-gib', type=float, default=6.0,
                        help='Required available RAM; 6 GiB suits 7B Q4 on CPU, smaller models need less')
    parser.add_argument('--num-gpu', type=int, choices=range(0, 1000), default=0, metavar='0-999',
                        help='Layers to offload to the GPU (worker CAG_OLLAMA_NUM_GPU)')
    parser.add_argument('--i-approve-gpu', action='store_true', help='Required for --num-gpu > 0')
    args = parser.parse_args()
    if args.num_gpu and not args.i_approve_gpu:
        parser.error('--num-gpu > 0 needs explicit operator approval (--i-approve-gpu)')
    with httpx.Client(trust_env=False, timeout=10) as client:
        installed = check_backend(client, args.model, args.min_free_gib, OLLAMA)
        # Keep the port reserved until the coordinator process inherits the socket.
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(128)
            base = f'http://127.0.0.1:{listener.getsockname()[1]}'
            admin, user = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            scratch = ROOT / '.pytest_cache' / 'ollama-smoke'
            scratch.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=scratch) as directory:
                env = {k: v for k, v in os.environ.items() if not k.startswith('CAG_')}
                env.update(CAG_ADMIN_KEY=admin, CAG_USER_KEY=user,
                           CAG_DB_PATH=str(Path(directory) / 'test.sqlite3'),
                           CAG_INITIAL_DEMO_CREDITS='100', CAG_ALLOWED_MODELS=args.model)
                proc = subprocess.Popen([
                    sys.executable, '-m', 'uvicorn', 'coordinator.app.main:app',
                    '--fd', str(listener.fileno()), '--workers', '1', '--no-access-log'],
                    cwd=ROOT, env=env, pass_fds=(listener.fileno(),),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    for _ in range(60):
                        if proc.poll() is not None:
                            raise RuntimeError('Test coordinator exited before becoming healthy')
                        try:
                            health = client.get(base + '/health', timeout=0.5)
                            if health.is_success:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.1)
                    else:
                        raise RuntimeError('Test coordinator startup timed out')
                    assert health.json()['verified_compute'] is False
                    enrolled = client.post(base + '/api/admin/workers',
                        headers={'Authorization': 'Bearer ' + admin},
                        json={'name': 'local-ollama-test', 'model': args.model, 'trust_level': 'trusted'})
                    enrolled.raise_for_status()
                    worker = enrolled.json()
                    user_headers = {'Authorization': 'Bearer ' + user}
                    submitted = client.post(base + '/api/jobs', headers=user_headers, json={
                        'model': args.model, 'privacy': 'trusted', 'max_output_tokens': 32,
                        'messages': [{'role': 'user', 'content': 'What is 2 + 2? Reply with the digit only.'}]})
                    submitted.raise_for_status()
                    worker_env = dict(env, CAG_COORD_URL=base, CAG_WORKER_TOKEN=worker['worker_token'],
                        CAG_MODEL=args.model, CAG_LLM_BACKEND='ollama', CAG_LLM_URL=OLLAMA + '/api/chat',
                        CAG_OLLAMA_NUM_CTX='1024', CAG_OLLAMA_NUM_THREAD='2', CAG_OLLAMA_MAX_OUTPUT_TOKENS='32',
                        CAG_OLLAMA_NUM_GPU=str(args.num_gpu))
                    started = time.monotonic()
                    result = subprocess.run([sys.executable, '-m', 'worker.client', '--once'],
                        cwd=ROOT, env=worker_env, timeout=150, capture_output=True, text=True)
                    elapsed = time.monotonic() - started
                    if result.returncode:
                        raise RuntimeError(f'Test worker exited with status {result.returncode}; no output published')
                    status = client.get(base + '/api/jobs/' + submitted.json()['id'], headers=user_headers)
                    status.raise_for_status()
                    job = status.json()
                    assert job['status'] == 'completed'
                    assert job['output']['content'].strip() == '4', 'Unexpected synthetic answer (withheld)'
                    usage = job['output']['usage']
                    assert 0 < usage['completion_tokens'] <= 32
                    balance = client.get(base + '/api/balance', headers=user_headers)
                    balance.raise_for_status()
                    assert balance.json()['demo_credits'] == 95
                    workers = client.get(base + '/api/admin/workers', headers={'Authorization': 'Bearer ' + admin})
                    workers.raise_for_status()
                    assert workers.json()[0]['provisional_credits'] == 5
                    owner = client.get(base + '/api/balance', headers={'Authorization': 'Bearer ' + worker['user_token']})
                    owner.raise_for_status()
                    assert owner.json()['demo_credits'] == 0
                    for _ in range(30):
                        unloaded = client.get(OLLAMA + '/api/ps')
                        unloaded.raise_for_status()
                        if not unloaded.json().get('models'):
                            break
                        time.sleep(0.1)
                    else:
                        raise RuntimeError('Backend still has loaded models; no forced stop attempted')
                    print(json.dumps({'status': 'PASS', 'backend': 'ollama', 'model': args.model,
                        'model_size_bytes': installed['size'], 'quantization': installed.get('details', {}).get('quantization_level'),
                        'num_gpu': args.num_gpu, 'worker_wall_seconds': round(elapsed, 3), 'usage_unverified': usage,
                        'user_balance': 95, 'worker_provisional_credits': 5, 'worker_spendable_credits': 0,
                        'models_loaded_after': 0, 'answer_check': 'PASS'}, indent=2))
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()


if __name__ == '__main__':
    main()
