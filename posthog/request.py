from datetime import date, datetime
from dateutil.tz import tzutc
import logging
import json
from gzip import GzipFile
from requests.auth import HTTPBasicAuth
import requests
from io import BytesIO

from posthog.version import VERSION
from posthog.utils import remove_trailing_slash

_session = requests.sessions.Session()

DEFAULT_HOST = 'https://app.posthog.com'
USER_AGENT = 'posthog-python/' + VERSION


def post(api_key, host=None, path=None, gzip=False, timeout=15, **kwargs):
    """Post the `kwargs` to the API"""
    log = logging.getLogger('posthog')
    body = kwargs
    body["sentAt"] = datetime.utcnow().replace(tzinfo=tzutc()).isoformat()
    url = remove_trailing_slash(host or DEFAULT_HOST) + path
    body['api_key'] = api_key
    data = json.dumps(body, cls=DatetimeSerializer)
    log.debug('making request: %s', data)
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': USER_AGENT
    }
    if gzip:
        headers['Content-Encoding'] = 'gzip'
        buf = BytesIO()
        with GzipFile(fileobj=buf, mode='w') as gz:
            # 'data' was produced by json.dumps(),
            # whose default encoding is utf-8.
            gz.write(data.encode('utf-8'))
        data = buf.getvalue()

    res = _session.post(url, data=data,
                        headers=headers, timeout=timeout)

    if res.status_code == 200:
        log.debug('data uploaded successfully')
        return res


def decide(api_key, host=None, gzip=False, timeout=15, **kwargs):
    """Post the `kwargs to the decide API endpoint"""
    log = logging.getLogger('posthog')
    res = post(api_key, host, '/decide/', gzip, timeout, **kwargs)
    if res.status_code == 200:
        log.debug('Feature flags decided successfully')
        return res.json()
    try:
        payload = res.json()
        log.debug('received response: %s', payload)
        raise APIError(res.status_code, payload['detail'])
    except ValueError:
        raise APIError(res.status_code, res.text)


def batch_post(api_key, host=None, gzip=False, timeout=15, **kwargs):
    """Post the `kwargs` to the batch API endpoint for events"""
    log = logging.getLogger('posthog')
    res = post(api_key, host, '/batch/', gzip, timeout, **kwargs)

    if res.status_code == 200:
        log.debug('data uploaded successfully')
        return res
    try:
        payload = res.json()
        log.debug('received response: %s', payload)
        raise APIError(res.status_code, payload['detail'])
    except ValueError:
        raise APIError(res.status_code, res.text)


def get(api_key, url, host=None, timeout=None):
    log = logging.getLogger('posthog')
    url = remove_trailing_slash(host or DEFAULT_HOST) + url
    response = requests.get(
        url,
        headers={
            'Authorization': 'Bearer %s' % api_key,
            'User-Agent': USER_AGENT
        },
        timeout=timeout
    )
    if response.status_code == 200:
        return response.json()
    try:
        payload = response.json()
        log.debug('received response: %s', payload)
        raise APIError(response.status_code, payload['detail'])
    except ValueError:
        raise APIError(response.status_code, response.text)


class APIError(Exception):

    def __init__(self, status, message):
        self.message = message
        self.status = status

    def __str__(self):
        msg = "[PostHog] {0} ({1})"
        return msg.format(self.message, self.status)


class DatetimeSerializer(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()

        return json.JSONEncoder.default(self, obj)
