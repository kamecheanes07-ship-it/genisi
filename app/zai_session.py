"""Experimental chat.z.ai web protocol (not the developer API).

Protocol informed by https://github.com/zunvein/glm and
https://github.com/kao0312/zai-proxy. The web protocol is not stable.
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import time
import uuid

BASE = 'https://chat.z.ai'
MODEL = 'x-preview-l'
SALT = 'key-@@@@)))()((9))-xxxx&&&%%%%%'


class _RedactChatURL(logging.Filter):
    def filter(self, record):
        # httpx logs full request URLs by default; the web protocol puts the
        # session token in the query string. Drop these records entirely.
        return 'chat.z.ai' not in record.getMessage()


logging.getLogger('httpx').addFilter(_RedactChatURL())


def user_id(token):
    # Decode only to construct the provider request; never trust this as auth.
    try:
        parts = token.split('.')
        if len(parts) != 3 or not all(parts):
            raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        uid = payload['id']
        if not isinstance(uid, str) or not uid or len(uid) > 200:
            raise ValueError()
        return uid
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise ValueError('Invalid Z.AI session token') from None


def request_parameters(token, uid, prompt):
    ts = str(int(time.time() * 1000))
    request_id = str(uuid.uuid4())
    key = hmac.new(SALT.encode(), str(int(ts) // 300000).encode(), hashlib.sha256).hexdigest()
    encoded = base64.b64encode(prompt.strip().encode()).decode()
    data = f'requestId,{request_id},timestamp,{ts},user_id,{uid}|{encoded}|{ts}'
    signature = hmac.new(key.encode(), data.encode(), hashlib.sha256).hexdigest()
    return {'timestamp': ts, 'requestId': request_id, 'user_id': uid, 'version': '0.0.1',
            'platform': 'web', 'token': token, 'signature_timestamp': ts}, signature


def split_text(raw, final=False):
    """Split the web UI's details blocks, withholding partial tags in flight."""
    reasoning, answer = [], []
    rest = raw
    while rest:
        i = rest.find('<details')
        if i < 0:
            if not final:
                for n in range(min(len(rest), 7), 0, -1):
                    if '<details'.startswith(rest[-n:]):
                        rest = rest[:-n]
                        break
            answer.append(rest)
            break
        answer.append(rest[:i])
        end = rest.find('>', i)
        if end < 0:
            break
        part = rest[end + 1:]
        close = part.find('</details>')
        if close < 0:
            if not final:
                for n in range(min(len(part), 9), 0, -1):
                    if '</details>'.startswith(part[-n:]):
                        part = part[:-n]
                        break
            reasoning.append(part)
            break
        reasoning.append(part[:close])
        rest = part[close + len('</details>'):]
    return ''.join(reasoning), ''.join(answer)


def update_text(raw, data):
    if isinstance(data.get('edit_content'), str):
        # Browser offsets count UTF-16 code units, not Python characters.
        index = data.get('edit_index', 0)
        if not isinstance(index, int) or index < 0:
            index = 0
        prefix = raw.encode('utf-16-le')[:index * 2].decode('utf-16-le', errors='ignore')
        return prefix + data['edit_content']
    if isinstance(data.get('content'), str):
        return data['content']
    if isinstance(data.get('delta_content'), str):
        return raw + data['delta_content']
    return raw
