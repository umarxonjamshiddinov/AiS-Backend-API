"""Local HTTP smoke test. Outputs only allowlisted result fields, never raw errors."""
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import dotenv_values

BASE_URL = 'http://127.0.0.1:8000'


def main():
    try:
        with urlopen(BASE_URL + '/health', timeout=10) as response:
            health = json.load(response)
        assert health == {'status': 'ok'}
        print('health: ok')
        request = Request(
            BASE_URL + '/internal/test/bedrock',
            data=json.dumps({'prompt': 'Reply with exactly OK', 'max_tokens': 16,
                             'temperature': 0}).encode(),
            headers={'Content-Type': 'application/json',
                     'Authorization': 'Bearer ' + dotenv_values(
                         Path(__file__).resolve().parents[1] / '.env').get('ADMIN_TOKEN', '')},
        )
        with urlopen(request, timeout=150) as response:
            result = json.load(response)
        assert result['text'].strip() == 'OK', 'Unexpected smoke response'
        assert result['provider'] == 'bedrock'
        print('bedrock: success; response=OK')
        print('model:', result['model'])
        print('usage:', result['usage'])
    except HTTPError as exc:
        print('smoke failed: HTTP', exc.code)
        return 1
    except (URLError, AssertionError, KeyError, ValueError):
        print('smoke failed: connection or response validation')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
