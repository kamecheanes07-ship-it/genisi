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
    if isinstance(data.get('edit_content'), str) and (data['edit_content'] or isinstance(data.get('edit_index'), int)):
        # Browser offsets count UTF-16 code units, not Python characters.
        index = data.get('edit_index', 0)
        if not isinstance(index, int) or index < 0:
            index = 0
        prefix = raw.encode('utf-16-le')[:index * 2].decode('utf-16-le', errors='ignore')
        return prefix + data['edit_content']
    # The provider can send an empty content field in a terminal frame.
    # It is not a replacement for the text already streamed.
    if isinstance(data.get('content'), str) and data['content']:
        return data['content']
    if isinstance(data.get('delta_content'), str):
        return raw + data['delta_content']
    return raw


def error_message(chunk, event='message'):
    """Classify embedded errors without returning provider data or secrets."""
    nodes = [chunk]
    node = chunk
    for _ in range(2):
        if not isinstance(node, dict) or not isinstance(node.get('data'), dict):
            break
        node = node['data']
        nodes.append(node)
    errors = []
    for node in nodes:
        err = node.get('error')
        if err:
            errors.append(err)
        if str(node.get('phase', '')).lower() in ('error', 'failed'):
            errors.append(node)
    if event == 'error' and not errors:
        errors.append(chunk)
    if not errors and chunk.get('code') not in (None, 0, 200):
        errors.append(chunk)
    if not errors:
        return None
    # Read only common error fields; never expose their raw values to clients.
    hints = []
    for err in errors:
        if isinstance(err, dict):
            hints.extend(str(err.get(key, ''))[:500].lower() for key in ('code', 'detail', 'message', 'type'))
        elif isinstance(err, str):
            hints.append(err[:500].lower())
    hint = ' '.join(hints)
    if any(x in hint for x in ('captcha', 'verification', 'verify', 'challenge', 'waf')):
        return 'طلبت Z.AI التحقق أو رفضت الطلب أمنياً. افتح الموقع وأكمل التحقق؛ قد لا يدعم الربط التجريبي التحقق المطلوب.'
    if any(x in hint for x in ('rate limit', 'rate_limit', 'quota', 'capacity', 'concurrency', 'too many')):
        return 'وصل حساب Z.AI إلى حد الطلبات أو سعة النموذج؛ انتظر وحاول لاحقاً.'
    if any(x in hint for x in ('unauthorized', 'invalid token', 'token expired', 'authentication', 'unauthenticated')):
        return 'رفضت Z.AI جلسة الحساب؛ جدّد توكن الجلسة في أسرار الاستضافة.'
    if 'model' in hint and any(x in hint for x in ('invalid', 'not found', 'unavailable', 'not supported')):
        return 'نموذج Carbon غير متاح لحساب Z.AI أو تغيّر معرّفه على الموقع.'
    return 'أعادت Z.AI خطأ داخل البث؛ تحقق من الجلسة وحدود الحساب والتحقق في الموقع.'
