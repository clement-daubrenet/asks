from numbers import Number
from os.path import basename
from urllib.parse import urlparse, urlunparse, quote
import json as _json
from random import randint
import mimetypes
import re

import h11
from asks import _async_lib

from .auth import PreResponseAuth, PostResponseAuth
from .req_structs import CaseInsensitiveDict as c_i_dict
from .response_objects import Response, StreamBody
from .errors import TooManyRedirects


__all__ = ['Request']


_BOUNDARY = "8banana133744910kmmr13ay5fa56" + str(randint(1e3, 9e3))
_WWX_MATCH = re.compile(r'\Aww.\.')
_MAX_BYTES = 4096


class Request:
    '''
    Handles the building, formatting and i/o of requests once the calling
    session passes the required info and calls `make_request`.

    Args:
        session (child of BaseSession): A refrence to the calling session.

        method (str): The HTTP method to be used in the request.

        uri (str): The full uri path to be requested. May not include query.

        port (str): The port we want to use on the net location.

        auth (child of AuthBase): An object for handling auth construction.

        data (dict or str): Info to be processed as a body-bound query.

        params (dict or str): Info to be processed as a url-bound query.

        headers (dict): User HTTP headers to be used in the request.

        encoding (str): The str representation of the codec to process the
            request under.

        json (dict): A dict to be formatted as json and sent in request body.

        files (dict): A dict of `filename:filepath`s to be sent as multipart.

        cookies (dict): A dict of `name:value` cookies to be passed in request.

        callback (func): A callback function to be called on each bytechunk of
            of the response body.

        timeout (int or float): A numeric representation of the longest time to
            wait on a complete response once a request has been sent.

        max_redirects (int): The maximum number of redirects allowed.

        persist_cookies (True or None): Passing True instanciates a
            CookieTracker object to manage the return of cookies to the server
            under the relevant domains.

        sock (StreamSock): The socket object to be used for the request. This
            socket object may be updated on `connection: close` headers.
    '''
    def __init__(self, session, method, uri, port, **kwargs):
        # These are kwargsable attribs.
        self.session = session
        self.method = method
        self.uri = uri
        self.port = port
        self.auth = None
        self.auth_off_domain = None
        self.data = None
        self.params = None
        self.headers = None
        self.encoding = None
        self.json = None
        self.files = None
        self.cookies = {}
        self.callback = None
        self.stream = None
        self.timeout = None
        self.max_redirects = 20
        self.sock = None
        self.persist_cookies = None

        self.__dict__.update(kwargs)

        # These are unkwargsable, and set by the code.
        self.history_objects = []
        self.scheme = None
        self.netloc = None
        self.path = None
        self.query = None
        self.target_netloc = None

        self.initial_scheme = None
        self.initial_netloc = None

        self.streaming = False

    async def make_request(self, redirect=False):
        '''
        Acts as the central hub for preparing requests to be sent, and
        returning them upon completion. Generally just pokes through
        self's attribs and makes decisions about what to do.

        Returns:
            sock: The socket to be returned to the calling session's
                pool.
            Response: The response object, after any redirects. If there were
                redirects, the redirect responses will be stored in the final
                response object's `.history`.
        '''
        hconnection = h11.Connection(our_role=h11.CLIENT)
        self.scheme, self.netloc, self.path, _, self.query, _ = urlparse(
            self.uri)

        if not redirect:
            self.initial_scheme = self.scheme
            self.initial_netloc = self.netloc

        host = (self.netloc if (self.port == '80' or
                                self.port == '443')
                else self.netloc + ':' + self.port)
        # default header construction
        asks_headers = c_i_dict([('Host', host),
                                 ('Connection', 'keep-alive'),
                                 ('Accept-Encoding', 'gzip, deflate'),
                                 ('Accept', '*/*'),
                                 ('Content-Length', '0'),
                                 ('User-Agent', 'python-asks/0.0.1')
                                 ])

        # check for a CookieTracker object, and if it's there inject
        # the relevant cookies in to the (next) request.
        if self.persist_cookies is not None:
            self.cookies.update(
                self.persist_cookies.get_additional_cookies(
                    self.netloc, self.path))

        # formulate path / query and intended extra querys for use in uri
        self._build_path()

        # handle building the request body, if any
        body = ''
        if any((self.data, self.files, self.json)):
            content_type, content_len, body = await self._formulate_body()
            asks_headers['Content-Type'] = content_type
            asks_headers['Content-Length'] = content_len

        # add custom headers, if any
        # note that custom headers take precedence
        if self.headers is not None:
            asks_headers.update(self.headers)

        # add auth
        if self.auth is not None:
            asks_headers.update(await self._auth_handler_pre())
            asks_headers.update(await self._auth_handler_post_get_auth())

        # add cookies
        if self.cookies:
            cookie_str = ''
            for k, v in self.cookies.items():
                cookie_str += '{}={}; '.format(k, v)
            asks_headers['Cookie'] = cookie_str[:-1]

        # Construct h11 request object.
        req = h11.Request(method=self.method,
                          target=self.path,
                          headers=asks_headers.items())
        # Construct h11 body object, if any body.
        if body:
            if not isinstance(body, bytes):
                body = bytes(body, self.encoding)
            req_body = h11.Data(data=body)
        else:
            req_body = None

        # call i/o handling func
        response_obj = await self._request_io(req, req_body, hconnection)

        # check to see if the final socket object is suitable to be returned
        # to the calling session's connection pool.
        # We don't want to return sockets that are of a difference schema or
        # different top level domain.
        if redirect:
            if not (self.scheme == self.initial_scheme and
               self.netloc == self.initial_netloc):
                self.sock._active = False

        if self.streaming:
            return None, response_obj

        return self.sock, response_obj

    async def _request_io(self, request_bytes, body_bytes, hconnection):
        '''
        Takes care of the i/o side of the request once it's been built,
        and calls a couple of cleanup functions to check for redirects / store
        cookies and the likes.

        Args:
            package (list): A list of strs representing HTTP headers. For
            example:
                'Connection: Keep-Alive'
            body (str): The str representation of the body to be sent in the
                request.

        Returns:
            Response: The final response object, including any response objects
                in `.history` generated by redirects.

        Notes:
            This function sets off a possible call to `_redirect` which
            is semi-recursive.
        '''
        await self._send(request_bytes, body_bytes, hconnection)
        response_obj = await self._catch_response(hconnection)
        response_obj._parse_cookies(self.netloc)

        # If there's a cookie tracker object, store any cookies we
        # might've picked up along our travels.
        if self.persist_cookies is not None:
            self.persist_cookies._store_cookies(response_obj)

        # Have a crack at guessing the encoding of the response.
        response_obj._guess_encoding()

        # Check to see if there's a PostResponseAuth set, and does magic.
        if self.auth is not None:
            response_obj = await self._auth_handler_post_check_retry(
                response_obj)

        # check redirects
        if self.method != 'HEAD':
            if self.max_redirects < 0:
                raise TooManyRedirects
            response_obj = await self._redirect(response_obj)
        response_obj.history = self.history_objects

        return response_obj

    def _build_path(self):
        '''
        Constructs the actual request URL with accompanying query if any.

        Returns:
            None: But does modify self.path, which contains the final
                request path sent to the server.

        '''
        if not self.path:
            self.path = '/'
        if self.query:
            self.path = self.path + '?' + self.query
        if self.params:
            try:
                if self.query:
                    self.path = self.path + self._dict_to_query(
                        self.params, base_query=True)
                else:
                    self.path = self.path + self._dict_to_query(self.params)
            except AttributeError:
                self.path = self.path + '?' + self._queryify(self.params)

    async def _redirect(self, response_obj):
        '''
        Calls the _check_redirect method of the supplied response object
        in order to determine if the http status code indicates a redirect.

        Returns:
            Response: May or may not be the result of recursive calls due
            to redirects!

        Notes:
            If it does redirect, it calls the appropriate method with the
            redirect location, returning the response object. Furthermore,
            if there is a redirect, this function is recursive in a roundabout
            way, storingthe previous response object in `.history_objects`.
        '''
        redirect, force_get, location = False, None, None
        if 300 <= response_obj.status_code < 400:
            if response_obj.status_code == 303:
                self.data, self.json, self.files = None, None, None
            if response_obj.status_code in [301, 305]:
                # redirect / force GET / location
                redirect = True
                force_get = False
            else:
                redirect = True
                force_get = True
            location = response_obj.headers['Location']

        if redirect:
            allow_redirect = True
            redirect_uri = urlparse(location.strip())
            # relative redirect
            if not redirect_uri.netloc:
                self.uri = urlunparse(
                    (self.scheme, self.netloc, *redirect_uri[2:]))

            # absolute-redirect
            else:
                location = location.strip()
                if self.auth is not None:
                    if not self.auth_off_domain:
                        allow_redirect = self._location_auth_protect(location)
                self.uri = location
                l_scheme, l_netloc, *_ = urlparse(location)
                if l_scheme != self.scheme or l_netloc != self.netloc:
                    await self._get_new_sock(off_base_loc=self.uri)

            # follow redirect with correct http method type
            if force_get:
                self.history_objects.append(response_obj)
                self.method = 'GET'
            else:
                self.history_objects.append(response_obj)
            self.max_redirects -= 1

            try:
                if response_obj.headers['connection'].lower() == 'close':
                    await self._get_new_sock()
            except KeyError:
                pass
            if allow_redirect:
                _, response_obj = await self.make_request()
        return response_obj

    async def _get_new_sock(self, off_base_loc=False):
        '''
        On 'Connetcion: close' headers we've to create a new connection.
        This reaches in to the parent session and pulls a switcheroo, dunking
        the current connection and requesting a new one.
        '''
        self.sock._active = False
        await self.session._replace_connection(self.sock)
        from asks.sessions import DSession
        if isinstance(self.session, DSession):
            self.sock = await self.session._grab_connection(
                self.uri)
            self.port = self.sock.port
        else:
            if not off_base_loc:
                self.sock = await self.session._grab_connection()
            else:
                self.sock, self.port = await self.session._grab_connection(
                    off_base_loc=off_base_loc)

    async def _formulate_body(self):
        '''
        Takes user suppied data / files and forms it / them
        appropriately, returning the contents type, len,
        and the request body its self.

        Returns:
            The str mime type for the Content-Type header.
            The len of the body.
            The body as a str.
        '''
        c_type, body = None, ''
        multipart_ctype = 'multipart/form-data; boundary={}'.format(_BOUNDARY)
        if self.files is not None and self.data is not None:
            c_type = multipart_ctype
            wombo_combo = {**self.files, **self.data}
            body = await self._multipart(wombo_combo)

        elif self.files is not None:
            c_type = multipart_ctype
            body = await self._multipart(self.files)

        elif self.data is not None:
            c_type = 'application/x-www-form-urlencoded'
            try:
                body = self._dict_to_query(self.data, params=False)
            except AttributeError:
                body = self.data
                c_type = ' text/html'

        elif self.json is not None:
            c_type = 'application/json'
            body = _json.dumps(self.json)

        return c_type, str(len(body)), body

    def _dict_to_query(self, data, params=True, base_query=False):
        '''
        Turns python dicts in to valid body-queries or queries for use directly
        in the request url. Unlike the stdlib quote() and it's variations,
        this also works on iterables like lists which are normally not valid.

        The use of lists in this manner is not a great idea unless
        the server supports it. Caveat emptor.

        Returns:
            Query part of url (or body).
        '''
        query = []

        for k, v in data.items():
            if not v:
                continue
            if isinstance(v, (str, Number)):
                query.append(self._queryify(
                    (k + '=' + '+'.join(str(v).split()))))
            elif isinstance(v, dict):
                for key in v:
                    query.append(self._queryify((k + '=' + key)))
            elif hasattr(v, '__iter__'):
                for elm in v:
                    query.append(
                        self._queryify((k + '=' +
                                       '+'.join(str(elm).split()))))

        if params and query:
            if not base_query:
                return '?' + '&'.join(query)
            else:
                return '&' + '&'.join(query)

        return '&'.join(query)

    async def _multipart(self, files_dict):
        '''
        Forms multipart requests from a dict with name, path k/vs. Name
        does not have to be the actual file name.

        Args:
            files_dict (dict): A dict of `filename:filepath`s, to be sent
            as multipart files.

        Returns:
            multip_pkg (str): The strings representation of the content body,
            multipart formatted.
        '''
        boundary = bytes(_BOUNDARY, self.encoding)
        hder_format = 'Content-Disposition: form-data; name="{}"'
        hder_format_io = '; filename="{}"'

        multip_pkg = b''

        num_of_parts = len(files_dict)

        for index, kv in enumerate(files_dict.items(), start=1):
            multip_pkg += (b'--' + boundary + b'\r\n')
            k, v = kv

            try:
                async with _async_lib.aopen(v, 'rb') as o_file:
                    pkg_body = b''.join(await o_file.readlines()) + b'\r\n'
                multip_pkg += bytes(hder_format.format(k) +
                                    hder_format_io.format(basename(v)),
                                    self.encoding)
                mime_type = mimetypes.guess_type(basename(v))
                if not mime_type[1]:
                    mime_type = 'application/octet-stream'
                else:
                    mime_type = '/'.join(mime_type)
                multip_pkg += bytes('; Content-Type: ' + mime_type,
                                    self.encoding)
                multip_pkg += b'\r\n'*2 + pkg_body

            except (TypeError, FileNotFoundError):
                pkg_body = bytes(v, self.encoding) + b'\r\n'
                multip_pkg += bytes(hder_format.format(k) +
                                    '\r\n'*2, self.encoding)
                multip_pkg += pkg_body

            if index == num_of_parts:
                multip_pkg += b'--' + boundary + b'--\r\n'
        return multip_pkg

    def _queryify(self, query):
        '''
        Turns stuff in to a valid url query.
        '''
        return quote(query.encode(self.encoding, errors='strict'),
                     safe='/=+?&')

    async def _catch_response(self, hconnection):
        '''
        Instanciates the parser which manages incoming data, first getting
        the headers, storing cookies, and then parsing the response's body,
        if any. Supports normal and chunked response bodies.

        This function also instances the Response class in which the response
        satus line, headers, cookies, and body is stored.

        It should be noted that in order to remain preformant, if the user
        wishes to do any file IO it should use async files or risk long wait
        times and risk connection issues server-side when using callbacks.

        If a callback is used, the response's body will be None.

        Returns:
            The most recent response object.
        '''
        response = await self._recv_event(hconnection)
        resp_data = {'encoding': self.encoding,
                     'method': self.method,
                     'status_code': response.status_code,
                     'reason_phrase': str(response.reason, 'utf-8'),
                     'http_version': str(response.http_version, 'utf-8'),
                     'headers': c_i_dict(
                        [(str(name, 'utf-8'), str(value, 'utf-8'))
                         for name, value in response.headers]),
                     'body': b''
                     }
        for header in response.headers:
            if header[0] == b'set-cookie':
                try:
                    resp_data['headers']['set-cookie'].append(str(header[1],
                                                                  'utf-8'))
                except (KeyError, AttributeError):
                    resp_data['headers']['set-cookie'] = [str(header[1],
                                                          'utf-8')]
        get_body = False
        try:
            if int(resp_data['headers']['content-length']) > 0:
                get_body = True
        except KeyError:
            if resp_data['headers']['transfer-encoding'] == 'chunked':
                get_body = True

        if get_body:
            if self.callback is not None:
                endof = await self._body_callback(hconnection)
            elif self.stream is not None:
                if 199 < resp_data['status_code'] < 300:
                    if not ((self.scheme == self.initial_scheme and
                            self.netloc == self.initial_netloc) or
                            resp_data['headers']['connection'] == 'close'):
                        self.sock._active = False
                    resp_data['body'] = StreamBody(self.session,
                                                   hconnection,
                                                   self.sock)
                    self.streaming = True
            else:
                while True:
                    data = await self._recv_event(hconnection)
                    if isinstance(data, h11.Data):
                        resp_data['body'] += data.data
                    elif isinstance(data, h11.EndOfMessage):
                        endof = data
                        assert isinstance(endof, h11.EndOfMessage)
                        break
        else:
            endof = await self._recv_event(hconnection)
            assert isinstance(endof, h11.EndOfMessage)

        return Response(**resp_data)

    async def _recv_event(self, hconnection):
        while True:
            event = hconnection.next_event()
            if event is h11.NEED_DATA:
                hconnection.receive_data((await self.sock.recv(10000)))
                continue
            return event

    async def _send(self, request_bytes, body_bytes, hconnection):
        '''
        Takes a package and body, combines then, then shoots 'em off in to
        the ether.

        Args:
            package (list of str): The header package.
            body (str): The str representation of the body.
        '''
        await self.sock.sendall(hconnection.send(request_bytes))
        if body_bytes is not None:
            await self.sock.sendall(hconnection.send(body_bytes))
        await self.sock.sendall(hconnection.send(h11.EndOfMessage()))

    async def _auth_handler_pre(self):
        '''
        If the user supplied auth does not rely on any response
        (is a PreResponseAuth object) then we call the auth's __call__
        returning a dict to update the request's headers with.
        '''
        # pylint: disable=not-callable
        if isinstance(self.auth, PreResponseAuth):
            return await self.auth(self)
        return {}

    async def _auth_handler_post_get_auth(self):
        '''
        If the user supplied auth does rely on a response
        (is a PostResponseAuth object) then we call the auth's __call__
        returning a dict to update the request's headers with, as long
        as there is an appropriate 401'd response object to calculate auth
        details from.
        '''
        # pylint: disable=not-callable
        if isinstance(self.auth, PostResponseAuth):
            if self.history_objects:
                authable_resp = self.history_objects[-1]
                if authable_resp.status_code == 401:
                    if not self.auth.auth_attempted:
                        self.auth.auth_attempted = True
                        return await self.auth(authable_resp, self)
        return {}

    async def _auth_handler_post_check_retry(self, response_obj):
        '''
        The other half of _auth_handler_post_check_retry (what a mouthfull).
        If auth has not yet been attempted and the most recent response
        object is a 401, we store that response object and retry the request
        in exactly the same manner as before except with the correct auth.

        If it fails a second time, we simply return the failed response.
        '''
        if isinstance(self.auth, PostResponseAuth):
            if response_obj.status_code == 401:
                if not self.auth.auth_attempted:
                    self.history_objects.append(response_obj)
                    r = await self.make_request()
                    self.auth.auth_attempted = False
                    return r
                else:
                    response_obj.history = self.history_objects
                    return response_obj
        return response_obj

    async def _location_auth_protect(self, location):
        '''
        Checks to see if the new location is
            1. The same top level domain
            2. As or more secure than the current connection type

        Returns:
            True (bool): If the current top level domain is the same
                and the connection type is equally or more secure.
                False otherwise.
        '''
        netloc_sans_port = self.netloc.split(':')[0]
        netloc_sans_port = netloc_sans_port.replace(
            (re.match(_WWX_MATCH, netloc_sans_port)[0]), '')

        base_domain = '.'.join(netloc_sans_port.split('.')[-2:])

        l_scheme, l_netloc, _, _, _, _ = urlparse(location)
        location_sans_port = l_netloc.split(':')[0]
        location_sans_port = location_sans_port.replace(
            (re.match(_WWX_MATCH, location_sans_port)[0]), '')

        location_domain = '.'.join(location_sans_port.split('.')[-2:])

        if base_domain == location_domain:
            if l_scheme < self.scheme:
                return False
            else:
                return True

    async def _body_callback(self, hconnection):
        '''
        A callback func to be supplied if the user wants to do something
        directly with the response body's stream.
        '''
        # pylint: disable=not-callable
        while True:
            next_event = await self._recv_event(hconnection)
            if isinstance(next_event, h11.Data):
                await self.callback(next_event.data)
            else:
                return next_event
