"""
Module implements the WeConnect Session handling.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

import json
import logging
import secrets
import hashlib
import base64

from urllib.parse import parse_qs, parse_qsl, urlparse

import requests
from requests.models import CaseInsensitiveDict

from oauthlib.common import add_params_to_uri, generate_nonce, to_unicode
from oauthlib.oauth2 import InsecureTransportError
from oauthlib.oauth2 import is_secure_transport

from carconnectivity.errors import AuthenticationError, RetrievalError, TemporaryAuthenticationError

from carconnectivity_connectors.volkswagen_na.auth.openid_session import AccessType
from carconnectivity_connectors.volkswagen_na.auth.vw_web_session import VWWebSession

if TYPE_CHECKING:
    from typing import Tuple, Dict


LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.volkswagen.auth")


class MyVWSession(VWWebSession):
    """
    MyVWSession class handles the authentication and session management for Volkswagen's myVW service.
    """
    def __init__(self, session_user, **kwargs) -> None:
        super(MyVWSession, self).__init__(client_id='59992128-69a9-42c3-8621-7942041ba824_MYVW_ANDROID',
                                               refresh_url='https://b-h-s.spr.us00.p.con-veh.net/oidc/v1/token',
                                               scope='openid',
                                               redirect_uri='kombi:///login',
                                               state=None,
                                               session_user=session_user,
                                               **kwargs)

        self.verifier = None
        self.challenge = None
        self.headers = CaseInsensitiveDict({
            'accept': '*/*',
            'content-type': 'application/json',
            'content-version': '1',
            'user-agent': 'Car-Net/60 CFNetwork/1121.2.2 Darwin/19.3.0',
            'accept-language': 'en-us',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache'
        })

    def request(
        self,
        method,
        url,
        data=None,
        headers=None,
        withhold_token=False,
        access_type=AccessType.ACCESS,
        token=None,
        timeout=None,
        **kwargs
    ):
        """Intercept all requests and add weconnect-trace-id header."""

        #import secrets
        #traceId = secrets.token_hex(16)
        #we_connect_trace_id = (traceId[:8] + '-' + traceId[8:12] + '-' + traceId[12:16] + '-' + traceId[16:20] + '-' + traceId[20:]).upper()
        #headers = headers or {}
        #headers['weconnect-trace-id'] = we_connect_trace_id

        return super(MyVWSession, self).request(
            method, url, headers=headers, data=data, withhold_token=withhold_token, access_type=access_type, token=token, timeout=timeout, **kwargs
        )

    def login(self):
        super(MyVWSession, self).login()
        # retrieve authorization URL
        authorization_url_str: str = self.authorization_url(url='https://b-h-s.spr.us00.p.con-veh.net/oidc/v1/authorize')
        # perform web authentication
        response = self.do_web_auth(authorization_url_str)
        print("Authentication response 1: ", response)
        # fetch tokens from web authentication response
        self.fetch_tokens('https://b-h-s.spr.us00.p.con-veh.net/oidc/v1/token',
                          authorization_response=response)

    def refresh(self) -> None:
        # refresh tokens from refresh endpoint
        token = self.token
        self.refresh_tokens(
            'https://b-h-s.spr.us00.p.con-veh.net/oidc/v1/token'
        )

    def authorization_url(self, url, state=None, **kwargs) -> str:
        if self.redirect_uri is None:
            raise AuthenticationError('Redirect URI is not set')


        self.verifier = secrets.token_hex(64).upper()
        self.challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode('ascii')).digest()).decode('ascii')
        #self.challenge = pkce.get_code_challenge(self.verifier)
        #self.verifier, self.challenge = pkce.generate_pkce_pair(code_verifier_length=64)
        params: list[Tuple[str, str]] = [(('redirect_uri', self.redirect_uri)),
                                         (('scope', 'openid')),
                                         (('prompt', 'login')),
                                         (('code_challenge', self.challenge)),
                                         (('state', self.state)),
                                         (('response_type', 'code')),
                                         (('client_id', self.client_id))]

        # add required parameters redirect_uri and nonce to the authorization URL
        auth_url: str = add_params_to_uri(url, params)
        try_login_response: requests.Response = self.get(auth_url, allow_redirects=False, access_type=AccessType.NONE)  # pyright: ignore reportCallIssue
        print('Login response 1 = ', try_login_response, try_login_response.content)
        print('Login response 1 headers ', try_login_response.headers)
        if try_login_response.status_code != requests.codes['found'] or 'Location' not in try_login_response.headers:
            raise AuthenticationError('Authorization URL could not be fetched due to WeConnect failure')
        # Redirect is URL to authorize
        redirect: str = try_login_response.headers['Location']
        query: str = urlparse(redirect).query
        query_params: Dict[str, str] = dict(parse_qsl(query))
        if 'state' in query_params:
            print('Setting state to ', query_params['state'])
            #self.state = query_params['state']
        if 'nonce' not in query_params:
            redirect += '&nonce=' + params[1][1]

        return redirect

    def fetch_tokens(
        self,
        token_url,
        authorization_response=None,
        **_
    ):
        """
        Fetches tokens using the given token URL using the tokens from authorization response.

        Args:
            token_url (str): The URL to request the tokens from.
            authorization_response (str, optional): The authorization response containing the tokens. Defaults to None.
            **_ : Additional keyword arguments.

        Returns:
            dict: A dictionary containing the fetched tokens if successful.
            None: If the tokens could not be fetched.

        Raises:
            TemporaryAuthenticationError: If the token request fails due to a temporary WeConnect failure.
        """
        print('Authorization response', authorization_response)
        url = urlparse(authorization_response)
        query = parse_qs(url.query)

        token_data = {
            'grant_type': "authorization_code",
            'code': query['code'][0],
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'code_verifier': self.verifier
        }
        
        #        'grant_type': 'authorization_code',
        #        'code': query['code'][0],
        #        'client_id': self.client_id,
        #        'redirect_uri': self.redirect_uri,
        #        'code_verifier': self.verifier
        #        }
        token_headers = {
            'user-agent': "Car-Net/60 CFNetwork/1121.2.2 Darwin/19.3.0",
            'content-type': 'application/x-www-form-urlencoded',
            'accept-language': 'en-us',
            'accept': '*/*',
            'accept-encoding': 'gzip, deflate, br'
            }
        print('Requesting token', token_data, token_headers)

        response = self.websession.post('https://b-h-s.spr.us00.p.con-veh.net/oidc/v1/token', data=token_data, headers=token_headers)

        #response = self.websession.post('https://b-h-s.spr.us00.p.con-veh.net/oidc/v1/token', data=token_data)
        print('Token response', response, response.content)
        print('Token request', response.request.body, response.request.headers)

        # take token from authorization response (those are stored in self.token now!)
        self.parse_from_body(response.content)
        #self.parse_from_fragment(response)

        if False and self.token is not None and all(key in self.token for key in ('state', 'id_token', 'access_token', 'code')):
            # Generate json body for token request
            body: str = json.dumps(
                {
                    'state': self.token['state'],
                    'id_token': self.token['id_token'],
                    'redirect_uri': self.redirect_uri,
                    'region': 'na',
                    'access_token': self.token['access_token'],
                    'authorizationCode': self.token['code'],
                })

            request_headers: CaseInsensitiveDict = self.headers  # pyright: ignore reportAssignmentType
            request_headers['accept'] = 'application/json'

            # request tokens from token_url
            token_response = self.post(token_url, headers=request_headers, data=body, allow_redirects=False,
                                       access_type=AccessType.ID)  # pyright: ignore reportCallIssue
            if token_response.status_code != requests.codes['ok']:
                raise TemporaryAuthenticationError(f'Token could not be fetched due to temporary WeConnect failure: {token_response.status_code}')
            # parse token from response body
            token = self.parse_from_body(token_response.text)

            return token
        return self.token
        return None

    def parse_from_body(self, token_response, state=None):
        """
            Fix strange token naming before parsing it with OAuthlib.
        """
        try:
            # Tokens are in body of response in json format
            token = json.loads(token_response)
        except json.decoder.JSONDecodeError as err:
            raise TemporaryAuthenticationError('Token could not be refreshed due to temporary WeConnect failure: json could not be decoded') from err
        # Fix token keys, we want access_token instead of accessToken
        if 'accessToken' in token:
            token['access_token'] = token.pop('accessToken')
        # Fix token keys, we want id_token instead of idToken
        if 'idToken' in token:
            token['id_token'] = token.pop('idToken')
        # Fix token keys, we want refresh_token instead of refreshToken
        if 'refreshToken' in token:
            token['refresh_token'] = token.pop('refreshToken')
        # generate json from fixed dict
        fixed_token_response = to_unicode(json.dumps(token)).encode("utf-8")
        # Let OAuthlib parse the token
        return super(MyVWSession, self).parse_from_body(token_response=fixed_token_response, state=state)

    def refresh_tokens(
        self,
        token_url,
        refresh_token=None,
        auth=None,
        timeout=None,
        headers=None,
        verify=True,
        proxies=None,
        **_
    ):
        """
        Refreshes the authentication tokens using the provided refresh token.
        Args:
            token_url (str): The URL to request new tokens from.
            refresh_token (str, optional): The refresh token to use. Defaults to None.
            auth (tuple, optional): Authentication credentials. Defaults to None.
            timeout (float or tuple, optional): How long to wait for the server to send data before giving up. Defaults to None.
            headers (dict, optional): Headers to include in the request. Defaults to None.
            verify (bool, optional): Whether to verify the server's TLS certificate. Defaults to True.
            proxies (dict, optional): Proxies to use for the request. Defaults to None.
            **_ (dict): Additional arguments.
        Raises:
            ValueError: If no token endpoint is set for auto_refresh.
            InsecureTransportError: If the token URL is not secure.
            AuthenticationError: If the server requests new authorization.
            TemporaryAuthenticationError: If the token could not be refreshed due to a temporary server failure.
            RetrievalError: If the status code from the server is not recognized.
        Returns:
            dict: The new tokens.
        """
        LOG.info('Refreshing tokens')
        if not token_url:
            raise ValueError("No token endpoint set for auto_refresh.")

        if not is_secure_transport(token_url):
            raise InsecureTransportError()

        # Store old refresh token in case no new one is given
        refresh_token = refresh_token or self.refresh_token

        if headers is None:
            headers = {
                'user-agent': "Car-Net/60 CFNetwork/1121.2.2 Darwin/19.3.0",
                'content-type': 'application/x-www-form-urlencoded',
                'accept-language': 'en-us',
                'accept': '*/*'
            }

        data = {
                'grant_type': 'refresh_token',
                'client_id': self.client_id,
                'code_verifier': self.verifier,
                'refresh_token': self.refresh_token
                }

        # Request new tokens using the refresh token
        token_response = self.post(
            token_url,
            data=data,
            auth=auth,
            timeout=timeout,
            headers=headers,
            verify=verify,
            withhold_token=False,  # pyright: ignore reportCallIssue
            proxies=proxies,
            access_type=AccessType.REFRESH  # pyright: ignore reportCallIssue
        )
        if token_response.status_code == requests.codes['unauthorized']:
            raise AuthenticationError('Refreshing tokens failed: Server requests new authorization')
        elif token_response.status_code in (requests.codes['internal_server_error'], requests.codes['service_unavailable'], requests.codes['gateway_timeout']):
            raise TemporaryAuthenticationError('Token could not be refreshed due to temporary WeConnect failure: {tokenResponse.status_code}')
        elif token_response.status_code == requests.codes['ok']:
            # parse new tokens from response
            self.parse_from_body(token_response.text)
            if self.token is not None and "refresh_token" not in self.token:
                LOG.debug("No new refresh token given. Re-using old.")
                self.token["refresh_token"] = refresh_token
            return self.token
        else:
            raise RetrievalError(f'Status Code from MyVW while refreshing tokens was: {token_response.status_code}')
