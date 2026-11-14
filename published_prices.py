"""Read-only HTTPS adapter for the public PublishedPrices file portal."""
import http.cookiejar
import json
import re
import urllib.parse
import urllib.request
from importer import ImportFailure, RemoteFile

BASE = 'https://url.publishedprices.co.il'


class PublishedPricesClient:
    def __init__(self, chain):
        self.chain = chain
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = None
        self.paths = {}

    def request(self, path, data=None):
        headers = {'User-Agent': 'Kabalator/0.3 public-price-transparency importer', 'Referer': BASE + '/file'}
        if data is not None:
            headers.update({'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': self.csrf or '', 'X-Requested-With': 'XMLHttpRequest'})
            data = urllib.parse.urlencode(data).encode()
        response = self.opener.open(urllib.request.Request(BASE + path, data=data, headers=headers), timeout=45)
        if urllib.parse.urlsplit(response.url).hostname != 'url.publishedprices.co.il':
            response.close(); raise ImportFailure('Unexpected portal redirect')
        return response

    @staticmethod
    def token(html):
        match = re.search(r'name="csrftoken"\s+content="([^"]+)"', html)
        if not match: raise ImportFailure('Portal CSRF token is missing')
        return match.group(1)

    def login(self):
        with self.request('/login') as response: html = response.read().decode('utf-8')
        self.csrf = self.token(html)
        # These are the chain's published read-only credentials, not user credentials.
        with self.request('/login/user', {'username': self.chain.username, 'password': '', 'csrftoken': self.csrf, 'r': '/', 'Submit': 'Sign in'}) as response:
            html = response.read().decode('utf-8')
            if urllib.parse.urlsplit(response.url).path != '/file': raise ImportFailure('Public portal login failed')
        self.csrf = self.token(html)

    def list(self):
        self.login()
        self.paths = {}
        result = []
        start = 0
        for page_number in range(20):
            with self.request('/file/json/dir', {'cd': self.chain.path, 'iDisplayStart': start, 'iDisplayLength': 1000,
                    'sEcho': start // 1000 + 1, 'iColumns': 5, 'sSearch': '', 'csrftoken': self.csrf}) as response:
                data = json.load(response)
            if data.get('error'): raise ImportFailure('Portal directory: ' + str(data['error'])[:100])
            page = data.get('aaData', [])
            for row in page:
                name = row.get('fname', '')
                value = row.get('value', '')
                if row.get('type') != 'file' or not re.fullmatch(r'[A-Za-z0-9_-]+\.(?:gz|xml)', name, re.I): continue
                if not any(code in name for code in self.chain.codes): continue
                # The official UI builds /file/d + the published absolute path.
                if value.startswith('/'):
                    path = value
                else:
                    path = self.chain.path.rstrip('/') + '/' + name
                if '..' in path.split('/'): raise ImportFailure('Unsafe portal file path')
                self.paths[name] = path
                result.append(RemoteFile(name, row.get('size'), row.get('time')))
            if start + len(page) >= int(data.get('iTotalRecords', 0)): return result
            if not page: raise ImportFailure('Incomplete portal pagination')
            start += len(page)
        raise ImportFailure('Portal directory exceeds safety limit')

    def source_url(self, remote):
        return BASE + '/file/d' + urllib.parse.quote(self.paths[remote.name], safe='/')

    def download(self, remote):
        path = '/file/d' + urllib.parse.quote(self.paths[remote.name], safe='/')
        with self.request(path) as response:
            value = response.read(32 * 1024 * 1024 + 1)
        if len(value) > 32 * 1024 * 1024: raise ImportFailure('Portal file exceeds 32 MB')
        if value.lstrip().lower().startswith((b'<!doctype html', b'<html')): raise ImportFailure('Portal returned HTML instead of a catalog')
        return value
