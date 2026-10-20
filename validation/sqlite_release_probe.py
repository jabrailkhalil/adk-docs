"""Exercise the released wheel through the real model-free API server."""
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import tempfile

URIS = ('memory://', 'sqlite://', 'sqlite:///', 'sqlite:///:memory:', 'sqlite:///sessions.db')
AGENT = '''from google.adk.agents import BaseAgent
from google.adk.events import Event
from google.genai import types

class EchoAgent(BaseAgent):
    async def _run_async_impl(self, ctx):
        yield Event(author=self.name, content=types.Content(role="model", parts=[types.Part(text="ok")]))

root_agent = EchoAgent(name="echo_agent")
'''

def server(uri, agents_dir, mode):
    from fastapi.testclient import TestClient
    from google.adk.cli.fast_api import get_fast_api_app

    assert importlib.metadata.version('google-adk') == '2.12.0'
    app = get_fast_api_app(
        agents_dir=agents_dir, session_service_uri=uri, web=False,
        use_local_storage=False, artifact_service_uri='memory://',
        memory_service_uri='memory://', trace_to_cloud=False, otel_to_cloud=False,
    )
    session_path = '/apps/echo_app/users/test_user/sessions/test_session'
    with TestClient(app) as client:
        if mode == 'create':
            created = client.post(session_path, json={'state': {'witness': uri}})
            assert created.status_code == 200, created.text
            for text in ('one', 'two'):
                response = client.post('/run', json={
                    'appName': 'echo_app', 'userId': 'test_user',
                    'sessionId': 'test_session',
                    'newMessage': {'role': 'user', 'parts': [{'text': text}]},
                })
                assert response.status_code == 200, response.text
                assert any(part.get('text') == 'ok' for event in response.json()
                           for part in (event.get('content') or {}).get('parts', [])), response.text
            stored = client.get(session_path)
            assert stored.status_code == 200, stored.text
            session = stored.json()
            assert session['state']['witness'] == uri, session
            assert [part['text'] for event in session['events']
                    for part in (event.get('content') or {}).get('parts', [])
                    if 'text' in part] == ['one', 'ok', 'two', 'ok'], session
        else:
            stored = client.get(session_path)
            if uri == 'sqlite:///sessions.db':
                assert stored.status_code == 200, stored.text
                assert stored.json()['state']['witness'] == uri, stored.text
                assert len(stored.json()['events']) == 4, stored.text
            else:
                assert stored.status_code == 404, stored.text
    print(json.dumps({'uri': uri, 'phase': mode, 'passed': True}))

def main():
    results = []
    with tempfile.TemporaryDirectory(prefix='adk-release-probe-') as folder:
        root = Path(folder)
        agents_dir = root / 'agents'
        package = agents_dir / 'echo_app'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('from . import agent\n', encoding='utf-8')
        (package / 'agent.py').write_text(AGENT, encoding='utf-8')
        for index, uri in enumerate(URIS):
            cwd = root / str(index)
            cwd.mkdir()
            for mode in ('create', 'restart'):
                proc = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                       'server', uri, str(agents_dir), mode],
                                      cwd=cwd, text=True, capture_output=True, timeout=90)
                if proc.returncode:
                    raise AssertionError(f'{uri} {mode}\n{proc.stdout}\n{proc.stderr}')
                result = json.loads(proc.stdout.strip().splitlines()[-1])
                results.append(result)
                print(json.dumps(result), flush=True)
    Path('release-probe.json').write_text(json.dumps({
        'version': importlib.metadata.version('google-adk'),
        'network_model_calls': False, 'cases': results,
        'scope': 'Real ASGI API server; two offline agent turns and a separate Python-process restart per URL. Not a live TCP deployment or LLM-provider test.',
    }, indent=2), encoding='utf-8')

if __name__ == '__main__':
    if len(sys.argv) > 1:
        server(*sys.argv[2:])
    else:
        main()
