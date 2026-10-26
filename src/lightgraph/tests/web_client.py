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
                ('POST', '/files'), ('POST', '/database/capabilities'), ('POST', '/database/connect'), ('POST', '/runs'), ('DELETE', '')}
                or method.upper() == 'PATCH' and re.fullmatch(r'/files/[^/]+/binding', suffix)
                or method.upper() == 'DELETE' and re.fullmatch(r'/(?:files/[^/]+|sources/[^/]+(?:/tables)?)', suffix))
            headers = dict(kwargs.get('headers') or {})
            if protected and not any(key.lower() == 'if-match' for key in headers):
                response = super().request('GET', f'/api/projects/{pid}/config')
                if response.status_code == 200:
                    headers['If-Match'] = response.headers['etag']
                    kwargs['headers'] = headers
        return super().request(method, url, **kwargs)
