# -*- coding: utf-8 -*-
from time import perf_counter
from typing import Optional
from typing import Union
from typing import List
from typing import Dict
from cytoolz import sliding_window


from httptools import parse_url
from urllib.parse import parse_qs, urlunparse
from ujson import loads as json_loads
from random import getrandbits

from jussi.request.jsonrpc import from_request as jsonrpc_from_request
from jussi.request.jsonrpc import JSONRPCRequest

# HTTP/1.1: https://www.w3.org/Protocols/rfc2616/rfc2616-sec7.html#sec7.2.1
# > If the media type remains unknown, the recipient SHOULD treat it
# > as type "application/octet-stream"
DEFAULT_HTTP_CONTENT_TYPE = "application/json"


class Empty:
    def __bool__(self):
        return False


_empty = Empty()

RawRequestDict = Dict[str, Union[str, float, int, list, dict]]
RawRequestList = List[RawRequestDict]
RawRequest = Union[RawRequestDict, RawRequestList]
SingleJsonRpcRequest = JSONRPCRequest
BatchJsonRpcRequest = List[SingleJsonRpcRequest]
JsonRpcRequest = Union[SingleJsonRpcRequest, BatchJsonRpcRequest]


class HTTPRequest:
    """HTTP request optimized for use in JSONRPC reverse proxy"""

    __slots__ = (
        'app', 'headers', 'version', 'method', 'transport',
        'body', 'parsed_json', 'parsed_jsonrpc',
        '_ip', '_parsed_url', 'uri_template', 'stream',
        '_socket', '_port', 'timings', '_log', 'is_batch_jrpc',
        'is_single_jrpc'
    )

    def __init__(self, url_bytes: bytes, headers: dict,
                 version: str, method: str, transport) -> None:
        self._parsed_url = parse_url(url_bytes)
        self.app = None

        self.headers = headers
        self.version = version
        self.method = method
        self.transport = transport

        # Init but do not inhale
        self.body = []
        self.parsed_json = _empty
        self.parsed_jsonrpc = _empty
        self.uri_template = None
        self.stream = None
        self.is_batch_jrpc = False
        self.is_single_jrpc = False

        self.timings = {'created': perf_counter()}
        self._log = _empty

    def __repr__(self) -> str:
        if self.method is None or not self.path:
            return '<{0}>'.format(self.__class__.__name__)
        return '<{0}: {1} {2}>'.format(self.__class__.__name__,
                                       self.method,
                                       self.path)

    @property
    def json(self) -> Optional[RawRequest]:
        if self.parsed_json is _empty:
            self.parsed_json = None
            try:
                if not self.body:
                    return self.parsed_json
                self.parsed_json = json_loads(self.body)
            except Exception:
                from jussi.errors import ParseError
                raise ParseError(http_request=self)
        return self.parsed_json

    @property
    def jsonrpc(self) -> Optional[JsonRpcRequest]:
        if self.parsed_jsonrpc is _empty:
            self.parsed_jsonrpc = None
            try:
                if self.method != 'POST':
                    return self.parsed_jsonrpc
                jsonrpc_request = self.json
                if isinstance(jsonrpc_request, dict):
                    self.parsed_jsonrpc = jsonrpc_from_request(self, 0,
                                                               jsonrpc_request)
                    self.is_single_jrpc = True
                elif isinstance(jsonrpc_request, list):
                    self.parsed_jsonrpc = [
                        jsonrpc_from_request(self, batch_index, req)
                        for batch_index, req in enumerate(jsonrpc_request)
                    ]
                    self.is_batch_jrpc = True
            except Exception:
                pass
        return self.parsed_jsonrpc

    @property
    def ip(self):
        if not hasattr(self, '_socket'):
            self._get_address()
        return self._ip

    @property
    def port(self):
        if not hasattr(self, '_socket'):
            self._get_address()
        return self._port

    @property
    def socket(self):
        if not hasattr(self, '_socket'):
            self._get_socket()
        return self._socket

    def _get_address(self):
        self._socket = (self.transport.get_extra_info('peername') or
                        (None, None))
        self._ip, self._port = self._socket

    @property
    def scheme(self):
        scheme = 'http'
        if self.transport.get_extra_info('sslcontext'):
            scheme += 's'
        return scheme

    @property
    def host(self):
        # it appears that httptools doesn't return the host
        # so pull it from the headers
        return self.headers.get('Host', '')

    @property
    def content_type(self):
        return self.headers.get('Content-Type', DEFAULT_HTTP_CONTENT_TYPE)

    @property
    def match_info(self):
        """return matched info after resolving route"""
        return self.app.router.get(self)[2]

    @property
    def path(self) -> str:
        return self._parsed_url.path.decode('utf-8')

    @property
    def query_string(self):
        if self._parsed_url.query:
            return self._parsed_url.query.decode('utf-8')
        else:
            return ''

    @property
    def url(self):
        return urlunparse((
            self.scheme,
            self.host,
            self.path,
            None,
            self.query_string,
            None))

    @property
    def timings_str(self) -> dict:
        try:
            return {t2[0]: t2[1] - t1[1] for t1, t2 in
                    sliding_window(2, self.timings.items())}
        except Exception:
            return {}

    @property
    def jussi_request_id(self) -> str:
        return self.headers.get('x-jussi-request-id',
                                '%(rid)018d' % {'rid': getrandbits(50)})

    @property
    def amzn_trace_id(self) -> Optional[str]:
        return self.headers.get('x-amzn-trace-id')