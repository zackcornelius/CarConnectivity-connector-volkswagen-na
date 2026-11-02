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
import time
import uuid
from datetime import datetime

import jwt

from urllib.parse import parse_qs, parse_qsl, urlparse

import requests
from requests.models import CaseInsensitiveDict

from oauthlib.common import add_params_to_uri, to_unicode
from oauthlib.oauth2 import InsecureTransportError
from oauthlib.oauth2 import is_secure_transport

from carconnectivity.errors import AuthenticationError, RetrievalError, TemporaryAuthenticationError

from carconnectivity_connectors.volkswagen_na.auth.openid_session import AccessType
from carconnectivity_connectors.volkswagen_na.auth.vw_web_session import VWWebSession

if TYPE_CHECKING:
    from typing import Any, Tuple, Dict


LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.volkswagen_na.auth")


class MyVWSession(VWWebSession):
    """
    MyVWSession class handles the authentication and session management for Volkswagen's myVW service.
    """

    def __init__(self, session_user, country="us", verifier=None, **kwargs) -> None:
        client_id = "59992128-69a9-42c3-8621-7942041ba824_MYVW_ANDROID"
        if country == "ca":
            client_id = "69eb3c39-d2be-4006-8197-37cc4971e8fe_MYVW_ANDROID"
        super(MyVWSession, self).__init__(
            client_id=client_id,
            refresh_url=f"https://b-h-s.spr.{country}00.p.con-veh.net/oidc/v1/token",
            scope="openid",
            redirect_uri="kombi:///login",
            state=None,
            session_user=session_user,
            **kwargs,
        )

        self.country = country

        self.verifier = verifier
        self.challenge = None

        # Last token set received from the vw-token-relay via MQTT (see set_relay_tokens).
        # login() prefers these over running our own (PI-guarded) authorization-code grant.
        self._relay_tokens: Dict[str, Any] | None = None
        # Bumped on every relay publish (usable or not); login() waits on changes to
        # notice a fresh phone login/refresh while it holds only a stale token set.
        self._relay_seq: int = 0
        # Set by the connector when an MQTT token relay is configured; login() then
        # waits briefly for the first relay message instead of burning a web login.
        self.relay_expected: bool = False

        # Identify as the myVW Android app; VW's edge drops clients without these headers.
        # x-app-uuid is stable per install (persisted); mobile session id is per process.
        self._app_uuid: str = self.metadata.get("app_uuid") or str(uuid.uuid4())
        self.metadata["app_uuid"] = self._app_uuid
        self._mobile_session_id: str = str(uuid.uuid4())
        self.headers = CaseInsensitiveDict(self._app_headers("application/json;charset=UTF-8"))

    # myVW Android app version; bump if VW starts rejecting it.
    APP_VERSION: str = "2026.5.27-9076"

    # Required on both token grants since 2026-07-30; VW checks presence, not validity (CA).
    # US endpoint validates the token — use set_play_integrity_token() to supply a real one
    # from an external relay.
    PLAY_INTEGRITY_TOKEN: str = "unavailable"

    def set_play_integrity_token(self, token: str) -> None:
        """Update the Play Integrity token used in token grant requests.
        Called by the connector when a relay supplies a real PI token via MQTT."""
        if token and token != "unavailable":  # nosec B105 — not a password, sentinel for "no PI token"
            self.PLAY_INTEGRITY_TOKEN = token
            LOG.info("Play Integrity token updated (len=%d)", len(token))

    def set_relay_tokens(self, payload: Dict[str, Any]) -> bool:
        """Adopt a complete token set supplied by the vw-token-relay (MQTT).

        The relay captures the real myVW Android app's tokens — access, id and
        refresh — straight from a successful token exchange on the phone. Using
        them directly avoids running our own authorization-code grant, which the
        US authorization server rejects (401 INVALID_REQUEST from /azs) no matter
        which Play Integrity token we relay along with it.

        Returns True if the payload carried a usable (non-expired) access token
        which is now installed in this session.
        """
        # Count every attempt so login() can tell whether the relay has published
        # anything new since the last (now stale) set was adopted.
        self._relay_seq += 1

        access_token = payload.get("access_token")
        if not access_token:
            return False

        # Determine expiry from the JWT claims; the relay may also carry an
        # "issued_at" timestamp which is more accurate than "now" for expires_in.
        try:
            claims = jwt.decode(access_token, options={"verify_signature": False})
        except (jwt.PyJWTError, TypeError, ValueError) as err:
            LOG.warning("Relay tokens ignored: access token is not a decodable JWT (%s)", err)
            return False
        exp = claims.get("exp")
        if exp is not None and exp <= time.time():
            LOG.info("Relay tokens ignored: access token already expired (%ds ago)", int(time.time() - exp))
            return False
        # openid_session.add_token() refuses any token inside a 120s refresh buffer
        # (TokenExpiredError). Adopting such a token would make the very next request
        # raise again right after login() "succeeded", so require a little more life.
        RELAY_TOKEN_MIN_LIFETIME_SECONDS = 150  # pylint: disable=invalid-name
        if exp is not None and exp <= time.time() + RELAY_TOKEN_MIN_LIFETIME_SECONDS:
            LOG.info("Relay tokens ignored: access token expires in %ds, less than %ds", int(exp - time.time()), RELAY_TOKEN_MIN_LIFETIME_SECONDS)
            return False

        token: Dict[str, Any] = {
            "access_token": access_token,
            "token_type": payload.get("token_type", "Bearer"),
        }
        if payload.get("id_token"):
            token["id_token"] = payload["id_token"]
        if payload.get("refresh_token"):
            token["refresh_token"] = payload["refresh_token"]
        if exp is not None:
            token["expires_at"] = exp
            token["expires_in"] = int(exp - time.time())
        issued_at = payload.get("issued_at")
        if issued_at and exp is not None:
            try:
                issued_ts = datetime.fromisoformat(issued_at).timestamp()
                token["expires_in"] = int(exp - issued_ts)
            except (TypeError, ValueError):
                pass

        self.token = token
        self._relay_tokens = dict(payload)
        # The relay tokens belong to the phone app's login for this same user;
        # take the identity from the token claims so x-user-id etc. line up.
        sub = claims.get("sub") or claims.get("ssoid")
        if sub and not self.user_id:
            self.user_id = sub
        # Refreshing these tokens must replay the verifier from the phone's original
        # login (the endpoint rejects a refresh with a foreign code_verifier), so
        # adopt the one the relay captured from the app's token request.
        if payload.get("code_verifier"):
            self.verifier = payload["code_verifier"]
        # Mark the session as freshly logged in so force_relogin_after (if set)
        # is measured from when we adopted the relay tokens.
        self.last_login = time.time()
        LOG.info(
            "Adopted token set from MQTT relay (access_token len=%d, id_token=%s, refresh_token=%s, expires in %ss)",
            len(access_token),
            "yes" if token.get("id_token") else "no",
            "yes" if token.get("refresh_token") else "no",
            token.get("expires_in", "?"),
        )
        return True

    def _app_headers(self, content_type: str) -> Dict[str, str]:
        """Headers the myVW Android app sends on its API/token calls."""
        return {
            "user-agent": "okhttp/5.3.2",
            "x-user-agent": "mobile-android",
            "x-app-version": self.APP_VERSION,
            "x-app-uuid": self._app_uuid,
            "x-mobile-session-id": self._mobile_session_id,
            "x-user-locale": f"en-{self.country.upper()}",
            "x-user-country": self.country.upper(),
            "x-app-device-model": "Pixel 7",
            "x-app-device-os": "35",
            "content-type": content_type,
            "accept-encoding": "gzip",
            "connection": "Keep-Alive",
        }

    def add_token(self, uri, body=None, headers=None, access_type=AccessType.ACCESS, token=None, **kwargs):
        """Add the Bearer token plus the x-user-id header the app sends on authenticated calls."""
        uri, return_headers, body = super().add_token(uri, body=body, headers=headers, access_type=access_type, token=token, **kwargs)
        if self.user_id:
            return_headers["x-user-id"] = self.user_id
        return uri, return_headers, body

    def get(self, url, *args, **kwargs):
        """GET request with automatic error handling via raise_for_status()."""
        response = super().get(url, *args, **kwargs)
        response.raise_for_status()
        return response

    def post(self, url, *args, **kwargs):
        """POST request with automatic error handling via raise_for_status()."""
        response = super().post(url, *args, **kwargs)
        response.raise_for_status()
        return response

    def put(self, url, *args, **kwargs):
        """PUT request with automatic error handling via raise_for_status()."""
        response = super().put(url, *args, **kwargs)
        response.raise_for_status()
        return response

    def delete(self, url, *args, **kwargs):
        """DELETE request with automatic error handling via raise_for_status()."""
        response = super().delete(url, *args, **kwargs)
        response.raise_for_status()
        return response

    RELAY_WAIT_SECONDS: int = 10

    def _wait_for_relay_tokens(self) -> None:
        """Wait briefly for the MQTT relay to (re-)publish a usable token set."""
        seq = self._relay_seq
        deadline = time.monotonic() + self.RELAY_WAIT_SECONDS
        while time.monotonic() < deadline:
            if self._relay_seq != seq and self._relay_tokens is not None:
                return
            time.sleep(0.2)

    def login(self):
        # If a token relay is configured but the first (retained) MQTT message has not
        # arrived yet, wait briefly for it instead of burning a web login whose token
        # exchange the US endpoint rejects with 401 anyway.
        if self.relay_expected and self._relay_tokens is None:
            self._wait_for_relay_tokens()
            if self._relay_tokens is None:
                LOG.warning("No token set received from MQTT relay within %ds, falling back to web login", self.RELAY_WAIT_SECONDS)
        # If the MQTT relay provided a token set, adopt it instead of running our own
        # authorization-code grant: the US authorization server rejects our grant with
        # 401 (CarnetSPAuthorizationServer /azs "Unauthorized exception") even when a
        # genuine Play Integrity token is included, but the phone app's own tokens work.
        # If the cached set has gone stale in the meantime, wait for the relay to
        # publish a fresh one (the phone app re-logs in / refreshes on its own) before
        # falling back to a web login that the US endpoint will reject anyway.
        if self._relay_tokens and not self.set_relay_tokens(self._relay_tokens):
            LOG.info("Login: cached relay token set is no longer usable, waiting for a fresh one")
            self._wait_for_relay_tokens()
        if self._relay_tokens:
            LOG.info("Login: using token set from MQTT relay")
            if self.set_relay_tokens(self._relay_tokens):
                return
            LOG.warning("Login: relay token set is no longer usable, falling back to web login")
        super(MyVWSession, self).login()
        # retrieve authorization URL
        authorization_url_str: str = self.authorization_url(url=f"https://b-h-s.spr.{self.country}00.p.con-veh.net/oidc/v1/authorize")
        # perform web authentication
        response = self.do_web_auth(authorization_url_str)
        # fetch tokens from web authentication response
        self.fetch_tokens(f"https://b-h-s.spr.{self.country}00.p.con-veh.net/oidc/v1/token", authorization_response=response)

    def refresh(self) -> None:
        # refresh tokens from refresh endpoint
        self.refresh_tokens(f"https://b-h-s.spr.{self.country}00.p.con-veh.net/oidc/v1/token")

    def authorization_url(self, url, state=None, **kwargs) -> str:
        if self.redirect_uri is None:
            raise AuthenticationError("Redirect URI is not set")

        self.verifier = secrets.token_hex(64).upper()
        self.challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode("ascii")).digest()).decode("ascii")
        # self.challenge = pkce.get_code_challenge(self.verifier)
        # self.verifier, self.challenge = pkce.generate_pkce_pair(code_verifier_length=64)
        params: list[Tuple[str, str]] = [
            (("redirect_uri", self.redirect_uri)),
            (("scope", "openid")),
            (("prompt", "login")),
            (("code_challenge", self.challenge)),
            (("state", self.state)),
            (("response_type", "code")),
            (("client_id", self.client_id)),
        ]

        # add required parameters redirect_uri and nonce to the authorization URL
        auth_url: str = add_params_to_uri(url, params)
        try_login_response: requests.Response = self.get(auth_url, allow_redirects=False, access_type=AccessType.NONE)  # pyright: ignore reportCallIssue
        if try_login_response.status_code != requests.codes["found"] or "Location" not in try_login_response.headers:
            raise AuthenticationError("Authorization URL could not be fetched due to WeConnect failure")
        # Redirect is URL to authorize
        redirect: str = try_login_response.headers["Location"]
        query: str = urlparse(redirect).query
        query_params: Dict[str, str] = dict(parse_qsl(query))
        if "state" in query_params:
            pass
            # self.state = query_params['state']
        if "nonce" not in query_params:
            redirect += "&nonce=" + params[1][1]

        return redirect

    def fetch_tokens(self, token_url, authorization_response=None, **_):
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
        url = urlparse(authorization_response)
        query = parse_qs(url.query)

        token_data = {
            "play_integrity_token": self.PLAY_INTEGRITY_TOKEN,
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "code_verifier": self.verifier,
        }

        #        'grant_type': 'authorization_code',
        #        'code': query['code'][0],
        #        'client_id': self.client_id,
        #        'redirect_uri': self.redirect_uri,
        #        'code_verifier': self.verifier
        #        }
        # Exchange the code using the app headers; access_type=NONE (no Bearer yet).
        if LOG.isEnabledFor(logging.DEBUG):
            # Log the grant parameters so we can verify the PI token actually flows into
            # the request. Token values are truncated — only prefix + length are logged.
            _safe_params = {k: (v[:24] + f"...(len={len(v)})") if isinstance(v, str) and len(v) > 32 else v for k, v in token_data.items()}
            LOG.debug("Token request params: %s", _safe_params)
        try:
            response = self.post(
                f"https://b-h-s.spr.{self.country}00.p.con-veh.net/oidc/v1/token",
                data=token_data,
                headers=self._app_headers("application/x-www-form-urlencoded"),
                access_type=AccessType.NONE,
            )
        except requests.HTTPError as http_error:
            if http_error.response is not None and http_error.response.status_code == requests.codes["unauthorized"]:
                # Dump the server's rejection details — VW's authorization server usually
                # explains the failure (error/error_description) in the 401 body.
                err_resp = http_error.response
                body_text = ""
                try:
                    body_text = err_resp.text[:2000]
                except Exception as body_err:  # pylint: disable=broad-exception-caught
                    body_text = f"<unreadable: {body_err}>"
                LOG.debug(
                    "Token request 401 response: headers=%s body=%s",
                    dict(err_resp.headers),
                    body_text,
                )
                # The US endpoint rejects the grant when the Play Integrity token is the
                # placeholder or invalid. With an MQTT PI token relay configured the real
                # token may arrive shortly, so report this as temporary and let the
                # connector retry instead of treating it as a fatal error.
                raise TemporaryAuthenticationError(
                    "Token request rejected (401): Play Integrity token missing or invalid — waiting for a token from the MQTT relay before retrying"
                ) from http_error
            raise

        # take token from authorization response (those are stored in self.token now!)
        self.parse_from_body(response.content)
        # self.parse_from_fragment(response)

        if False and self.token is not None and all(key in self.token for key in ("state", "id_token", "access_token", "code")):
            # Generate json body for token request
            body: str = json.dumps(
                {
                    "state": self.token["state"],
                    "id_token": self.token["id_token"],
                    "redirect_uri": self.redirect_uri,
                    "region": "na",
                    "access_token": self.token["access_token"],
                    "authorizationCode": self.token["code"],
                }
            )

            request_headers: CaseInsensitiveDict = self.headers  # pyright: ignore reportAssignmentType
            request_headers["accept"] = "application/json"

            # request tokens from token_url
            token_response = self.post(token_url, headers=request_headers, data=body, allow_redirects=False, access_type=AccessType.ID)  # pyright: ignore reportCallIssue
            if token_response.status_code != requests.codes["ok"]:
                raise TemporaryAuthenticationError(f"Token could not be fetched due to temporary WeConnect failure: {token_response.status_code}")
            # parse token from response body
            token = self.parse_from_body(token_response.text)

            return token
        return self.token

    def parse_from_body(self, token_response, state=None):
        """
        Fix strange token naming before parsing it with OAuthlib.
        """
        try:
            # Tokens are in body of response in json format
            token = json.loads(token_response)
        except json.decoder.JSONDecodeError as err:
            raise TemporaryAuthenticationError("Token could not be refreshed due to temporary WeConnect failure: json could not be decoded") from err
        # Fix token keys, we want access_token instead of accessToken
        if "accessToken" in token:
            token["access_token"] = token.pop("accessToken")
        # Fix token keys, we want id_token instead of idToken
        if "idToken" in token:
            token["id_token"] = token.pop("idToken")
        # Fix token keys, we want refresh_token instead of refreshToken
        if "refreshToken" in token:
            token["refresh_token"] = token.pop("refreshToken")
        # generate json from fixed dict
        fixed_token_response = to_unicode(json.dumps(token)).encode("utf-8")
        # Let OAuthlib parse the token
        return super(MyVWSession, self).parse_from_body(token_response=fixed_token_response, state=state)

    def refresh_tokens(self, token_url, refresh_token=None, auth=None, timeout=None, headers=None, verify=True, proxies=None, **_):
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
        LOG.info("Refreshing tokens")
        if not token_url:
            raise ValueError("No token endpoint set for auto_refresh.")

        if not is_secure_transport(token_url):
            raise InsecureTransportError()

        # Store old refresh token in case no new one is given
        refresh_token = refresh_token or self.refresh_token

        if headers is None:
            headers = self._app_headers("application/x-www-form-urlencoded")

        # VW rejects the refresh grant with 400 unless the original login's verifier is replayed.
        if not self.verifier:
            raise AuthenticationError("Refreshing tokens failed: no code_verifier stored for this session, new authorization required")

        data = {
            "play_integrity_token": self.PLAY_INTEGRITY_TOKEN,
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "code_verifier": self.verifier,
            "refresh_token": self.refresh_token,
        }

        # Request new tokens using the refresh token
        try:
            token_response = self.post(
                token_url,
                data=data,
                auth=auth,
                timeout=timeout,
                headers=headers,
                verify=verify,
                withhold_token=False,  # pyright: ignore reportCallIssue
                proxies=proxies,
                access_type=AccessType.NONE,  # pyright: ignore reportCallIssue
            )
        except requests.HTTPError as http_error:
            if http_error.response.status_code == requests.codes["unauthorized"]:
                raise AuthenticationError("Refreshing tokens failed: Server requests new authorization")
            elif http_error.response.status_code in (
                requests.codes["internal_server_error"],
                requests.codes["service_unavailable"],
                requests.codes["gateway_timeout"],
            ):
                raise TemporaryAuthenticationError(f"Token could not be refreshed due to temporary WeConnect failure: {http_error.response.status_code}")
            else:
                raise RetrievalError(f"Status Code from MyVW while refreshing tokens was: {http_error.response.status_code}")
        self.parse_from_body(token_response.text)
        if self.token is not None and "refresh_token" not in self.token:
            LOG.debug("No new refresh token given. Re-using old.")
            self.token["refresh_token"] = refresh_token
        return self.token
