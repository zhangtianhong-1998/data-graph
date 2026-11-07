"""Latest-revision client for existing single-writer HTTP behavior tests.

Concurrency/missing-precondition tests use FastAPI's raw TestClient. This helper
never retries a failed write and never replaces an explicitly supplied If-Match.
"""
import re
from fastapi.testclient import TestClient


class RevisionClient(TestClient):
    def request(self, method, url, **kwargs):
        path = str(url).split('?', 1)[0]
        match = re.fullmatch(r'/api/projects/([^/]+)(.*)', path)
        if match:
            pid, suffix = match.groups()
            operation = (method.upper(), suffix)
            protected = (operation in {('PUT', '/config'), ('POST', '/config/import'),
                ('POST', '/files'), ('POST', '/database/capabilities'), ('POST', '/database/connect'), ('POST', '/runs'), ('DELETE', ''),
                ('PUT', '/credentials/model'), ('DELETE', '/credentials/model')}
                or method.upper() == 'PATCH' and re.fullmatch(r'/files/[^/]+/binding', suffix)
                or method.upper() == 'DELETE' and re.fullmatch(r'/(?:files/[^/]+|sources/[^/]+(?:/tables)?)', suffix))
            headers = dict(kwargs.get('headers') or {})
            if protected and not any(key.lower() == 'if-match' for key in headers):
                response = super().request('GET', f'/api/projects/{pid}/config')
                if response.status_code == 200:
                    headers['If-Match'] = response.headers['etag']
                    kwargs['headers'] = headers
            # Existing single-writer tests use the model they just configured.
            # Race and missing-precondition tests use the raw TestClient.
            if operation == ('PUT', '/credentials/model') and 'expected_model' not in (kwargs.get('json') or {}):
                response = super().request('GET', f'/api/projects/{pid}/config')
                if response.status_code == 200:
                    model = response.json().get('model', {})
                    kwargs['json'] = {**(kwargs.get('json') or {}), 'expected_model': {
                        'base_url': model.get('base_url', ''), 'model': model.get('model', '')}}
        return super().request(method, url, **kwargs)
