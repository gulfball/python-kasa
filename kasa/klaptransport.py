"""Implementation of the TP-Link Klap Home Protocol.

Encryption/Decryption methods based on the works of
Simon Wilkinson and Chris Weeldon

Klap devices that have never been connected to the kasa
cloud should work with blank credentials.
Devices that have been connected to the kasa cloud will
switch intermittently between the users cloud credentials
and default kasa credentials that are hardcoded.
This appears to be an issue with the devices.

The protocol works by doing a two stage handshake to obtain
and encryption key and session id cookie.

Authentication uses an auth_hash which is
md5(md5(username),md5(password))

handshake1: client sends a random 16 byte local_seed to the
device and receives a random 16 bytes remote_seed, followed
by sha256(local_seed + auth_hash).  It also returns a
TP_SESSIONID in the cookie header.  This implementation
then checks this value against the possible auth_hashes
described above (user cloud, kasa hardcoded, blank).  If it
finds a match it moves onto handshake2

handshake2: client sends sha25(remote_seed + auth_hash) to
the device along with the TP_SESSIONID.  Device responds with
200 if successful.  It generally will be because this
implementation checks the auth_hash it received during handshake1

encryption: local_seed, remote_seed and auth_hash are now used
for encryption.  The last 4 bytes of the initialization vector
are used as a sequence number that increments every time the
client calls encrypt and this sequence number is sent as a
url parameter to the device along with the encrypted payload

https://gist.github.com/chriswheeldon/3b17d974db3817613c69191c0480fe55
https://github.com/python-kasa/python-kasa/pull/117

"""

import asyncio
import base64
import datetime
import hashlib
import logging
import secrets
import struct
import time
from pprint import pformat as pf
from typing import Any, Dict, Optional, Tuple, cast

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from yarl import URL

from .credentials import Credentials
from .deviceconfig import DeviceConfig
from .exceptions import AuthenticationError, KasaException
from .httpclient import HttpClient
from .json import loads as json_loads
from .protocol import DEFAULT_CREDENTIALS, BaseTransport, get_default_credentials, md5

_LOGGER = logging.getLogger(__name__)


ONE_DAY_SECONDS = 86400
SESSION_EXPIRE_BUFFER_SECONDS = 60 * 20

PACK_SIGNED_LONG = struct.Struct(">l").pack


def _sha256(payload: bytes) -> bytes:
    return hashlib.sha256(payload).digest()  # noqa: S324


def _sha1(payload: bytes) -> bytes:
    return hashlib.sha1(payload).digest()  # noqa: S324


class KlapTransport(BaseTransport):
    """Implementation of the KLAP encryption protocol.

    KLAP is the name used in device discovery for TP-Link's new encryption
    protocol, used by newer firmware versions.
    """

    DEFAULT_PORT: int = 80
    DISCOVERY_QUERY = {"system": {"get_sysinfo": None}}
    SESSION_COOKIE_NAME = "TP_SESSIONID"
    TIMEOUT_COOKIE_NAME = "TIMEOUT"

    def __init__(
        self,
        *,
        config: DeviceConfig,
    ) -> None:
        super().__init__(config=config)

        self._http_client = HttpClient(config)
        self._local_seed: Optional[bytes] = None
        if (
            not self._credentials or self._credentials.username is None
        ) and not self._credentials_hash:
            self._credentials = Credentials()
        if self._credentials:
            self._local_auth_hash = self.generate_auth_hash(self._credentials)
            self._local_auth_owner = self.generate_owner_hash(self._credentials).hex()
        else:
            self._local_auth_hash = base64.b64decode(self._credentials_hash.encode())  # type: ignore[union-attr]
        self._default_credentials_auth_hash: Dict[str, bytes] = {}
        self._blank_auth_hash = None
        self._handshake_lock = asyncio.Lock()
        self._query_lock = asyncio.Lock()
        self._handshake_done = False

        self._encryption_session: Optional[KlapEncryptionSession] = None
        self._session_expire_at: Optional[float] = None

        self._session_cookie: Optional[Dict[str, Any]] = None

        _LOGGER.debug("Created KLAP transport for %s", self._host)
        self._app_url = URL(f"http://{self._host}:{self._port}/app")
        self._request_url = self._app_url / "request"

    @property
    def default_port(self):
        """Default port for the transport."""
        return self.DEFAULT_PORT

    @property
    def credentials_hash(self) -> str:
        """The hashed credentials used by the transport."""
        return base64.b64encode(self._local_auth_hash).decode()

    async def perform_handshake1(self) -> Tuple[bytes, bytes, bytes]:
        """Perform handshake1."""
        local_seed: bytes = secrets.token_bytes(16)

        # Handshake 1 has a payload of local_seed
        # and a response of 16 bytes, followed by
        # sha256(remote_seed | auth_hash)

        payload = local_seed

        url = self._app_url / "handshake1"

        response_status, response_data = await self._http_client.post(url, data=payload)

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Handshake1 posted at %s. Host is %s, Response"
                + "status is %s, Request was %s",
                datetime.datetime.now(),
                self._host,
                response_status,
                payload.hex(),
            )

        if response_status != 200:
            raise KasaException(
                f"Device {self._host} responded with {response_status} to handshake1"
            )

        response_data = cast(bytes, response_data)
        remote_seed: bytes = response_data[0:16]
        server_hash = response_data[16:]

        if len(server_hash) != 32:
            raise KasaException(
                f"Device {self._host} responded with unexpected klap response "
                + f"{response_data!r} to handshake1"
            )

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Handshake1 success at %s. Host is %s, "
                + "Server remote_seed is: %s, server hash is: %s",
                datetime.datetime.now(),
                self._host,
                remote_seed.hex(),
                server_hash.hex(),
            )

        local_seed_auth_hash = self.handshake1_seed_auth_hash(
            local_seed, remote_seed, self._local_auth_hash
        )  # type: ignore

        # Check the response from the device with local credentials
        if local_seed_auth_hash == server_hash:
            _LOGGER.debug("handshake1 hashes match with expected credentials")
            return local_seed, remote_seed, self._local_auth_hash  # type: ignore

        # Now check against the default setup credentials
        for key, value in DEFAULT_CREDENTIALS.items():
            if key not in self._default_credentials_auth_hash:
                default_credentials = get_default_credentials(value)
                self._default_credentials_auth_hash[key] = self.generate_auth_hash(
                    default_credentials
                )

            default_credentials_seed_auth_hash = self.handshake1_seed_auth_hash(
                local_seed,
                remote_seed,
                self._default_credentials_auth_hash[key],  # type: ignore
            )

            if default_credentials_seed_auth_hash == server_hash:
                _LOGGER.debug(
                    "Server response doesn't match our expected hash on ip %s"
                    + f" but an authentication with {key} default credentials matched",
                    self._host,
                )
                return local_seed, remote_seed, self._default_credentials_auth_hash[key]  # type: ignore

        # Finally check against blank credentials if not already blank
        blank_creds = Credentials()
        if self._credentials != blank_creds:
            if not self._blank_auth_hash:
                self._blank_auth_hash = self.generate_auth_hash(blank_creds)

            blank_seed_auth_hash = self.handshake1_seed_auth_hash(
                local_seed,
                remote_seed,
                self._blank_auth_hash,  # type: ignore
            )

            if blank_seed_auth_hash == server_hash:
                _LOGGER.debug(
                    "Server response doesn't match our expected hash on ip %s"
                    + " but an authentication with blank credentials matched",
                    self._host,
                )
                return local_seed, remote_seed, self._blank_auth_hash  # type: ignore

        msg = f"Server response doesn't match our challenge on ip {self._host}"
        _LOGGER.debug(msg)
        raise AuthenticationError(msg)

    async def perform_handshake2(
        self, local_seed, remote_seed, auth_hash
    ) -> "KlapEncryptionSession":
        """Perform handshake2."""
        # Handshake 2 has the following payload:
        #    sha256(serverBytes | authenticator)

        url = self._app_url / "handshake2"

        payload = self.handshake2_seed_auth_hash(local_seed, remote_seed, auth_hash)

        response_status, _ = await self._http_client.post(
            url,
            data=payload,
            cookies_dict=self._session_cookie,
        )

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Handshake2 posted %s.  Host is %s, Response status is %s, "
                + "Request was %s",
                datetime.datetime.now(),
                self._host,
                response_status,
                payload.hex(),
            )

        if response_status != 200:
            # This shouldn't be caused by incorrect
            # credentials so don't raise AuthenticationError
            raise KasaException(
                f"Device {self._host} responded with {response_status} to handshake2"
            )

        return KlapEncryptionSession(local_seed, remote_seed, auth_hash)

    async def perform_handshake(self) -> Any:
        """Perform handshake1 and handshake2.

        Sets the encryption_session if successful.
        """
        _LOGGER.debug("Starting handshake with %s", self._host)
        self._handshake_done = False
        self._session_expire_at = None
        self._session_cookie = None

        local_seed, remote_seed, auth_hash = await self.perform_handshake1()
        http_client = self._http_client
        if cookie := http_client.get_cookie(self.SESSION_COOKIE_NAME):  # type: ignore
            self._session_cookie = {self.SESSION_COOKIE_NAME: cookie}
        # The device returns a TIMEOUT cookie on handshake1 which
        # it doesn't like to get back so we store the one we want
        timeout = int(
            http_client.get_cookie(self.TIMEOUT_COOKIE_NAME) or ONE_DAY_SECONDS
        )
        # There is a 24 hour timeout on the session cookie
        # but the clock on the device is not always accurate
        # so we set the expiry to 24 hours from now minus a buffer
        self._session_expire_at = time.time() + timeout - SESSION_EXPIRE_BUFFER_SECONDS
        self._encryption_session = await self.perform_handshake2(
            local_seed, remote_seed, auth_hash
        )
        self._handshake_done = True

        _LOGGER.debug("Handshake with %s complete", self._host)

    def _handshake_session_expired(self):
        """Return true if session has expired."""
        return (
            self._session_expire_at is None
            or self._session_expire_at - time.time() <= 0
        )

    async def send(self, request: str):
        """Send the request."""
        if not self._handshake_done or self._handshake_session_expired():
            await self.perform_handshake()

        # Check for mypy
        if self._encryption_session is not None:
            payload, seq = self._encryption_session.encrypt(request.encode())

        response_status, response_data = await self._http_client.post(
            self._request_url,
            params={"seq": seq},
            data=payload,
            cookies_dict=self._session_cookie,
        )

        msg = (
            f"Host is {self._host}, "
            + f"Sequence is {seq}, "
            + f"Response status is {response_status}, Request was {request}"
        )
        if response_status != 200:
            _LOGGER.error("Query failed after successful authentication " + msg)
            # If we failed with a security error, force a new handshake next time.
            if response_status == 403:
                self._handshake_done = False
                raise AuthenticationError(
                    f"Got a security error from {self._host} after handshake "
                    + "completed"
                )
            else:
                raise KasaException(
                    f"Device {self._host} responded with {response_status} to"
                    + f"request with seq {seq}"
                )
        else:
            _LOGGER.debug("Query posted " + msg)

            # Check for mypy
            if self._encryption_session is not None:
                decrypted_response = self._encryption_session.decrypt(response_data)

            json_payload = json_loads(decrypted_response)

            _LOGGER.debug(
                "%s << %s",
                self._host,
                _LOGGER.isEnabledFor(logging.DEBUG) and pf(json_payload),
            )

            return json_payload

    async def close(self) -> None:
        """Close the http client and reset internal state."""
        await self.reset()
        await self._http_client.close()

    async def reset(self) -> None:
        """Reset internal handshake state."""
        self._handshake_done = False

    @staticmethod
    def generate_auth_hash(creds: Credentials):
        """Generate an md5 auth hash for the protocol on the supplied credentials."""
        un = creds.username
        pw = creds.password

        return md5(md5(un.encode()) + md5(pw.encode()))

    @staticmethod
    def handshake1_seed_auth_hash(
        local_seed: bytes, remote_seed: bytes, auth_hash: bytes
    ):
        """Generate an md5 auth hash for the protocol on the supplied credentials."""
        return _sha256(local_seed + auth_hash)

    @staticmethod
    def handshake2_seed_auth_hash(
        local_seed: bytes, remote_seed: bytes, auth_hash: bytes
    ):
        """Generate an md5 auth hash for the protocol on the supplied credentials."""
        return _sha256(remote_seed + auth_hash)

    @staticmethod
    def generate_owner_hash(creds: Credentials):
        """Return the MD5 hash of the username in this object."""
        un = creds.username
        return md5(un.encode())


class KlapTransportV2(KlapTransport):
    """Implementation of the KLAP encryption protocol with v2 hanshake hashes."""

    @staticmethod
    def generate_auth_hash(creds: Credentials):
        """Generate an md5 auth hash for the protocol on the supplied credentials."""
        un = creds.username
        pw = creds.password

        return _sha256(_sha1(un.encode()) + _sha1(pw.encode()))

    @staticmethod
    def handshake1_seed_auth_hash(
        local_seed: bytes, remote_seed: bytes, auth_hash: bytes
    ):
        """Generate an md5 auth hash for the protocol on the supplied credentials."""
        return _sha256(local_seed + remote_seed + auth_hash)

    @staticmethod
    def handshake2_seed_auth_hash(
        local_seed: bytes, remote_seed: bytes, auth_hash: bytes
    ):
        """Generate an md5 auth hash for the protocol on the supplied credentials."""
        return _sha256(remote_seed + local_seed + auth_hash)


class KlapEncryptionSession:
    """Class to represent an encryption session and it's internal state.

    i.e. sequence number which the device expects to increment.
    """

    _cipher: Cipher

    def __init__(self, local_seed, remote_seed, user_hash):
        self.local_seed = local_seed
        self.remote_seed = remote_seed
        self.user_hash = user_hash
        self._key = self._key_derive(local_seed, remote_seed, user_hash)
        (self._iv, self._seq) = self._iv_derive(local_seed, remote_seed, user_hash)
        self._aes = algorithms.AES(self._key)
        self._sig = self._sig_derive(local_seed, remote_seed, user_hash)

    def _key_derive(self, local_seed, remote_seed, user_hash):
        payload = b"lsk" + local_seed + remote_seed + user_hash
        return hashlib.sha256(payload).digest()[:16]

    def _iv_derive(self, local_seed, remote_seed, user_hash):
        # iv is first 16 bytes of sha256, where the last 4 bytes forms the
        # sequence number used in requests and is incremented on each request
        payload = b"iv" + local_seed + remote_seed + user_hash
        fulliv = hashlib.sha256(payload).digest()
        seq = int.from_bytes(fulliv[-4:], "big", signed=True)
        return (fulliv[:12], seq)

    def _sig_derive(self, local_seed, remote_seed, user_hash):
        # used to create a hash with which to prefix each request
        payload = b"ldk" + local_seed + remote_seed + user_hash
        return hashlib.sha256(payload).digest()[:28]

    def _generate_cipher(self):
        iv_seq = self._iv + PACK_SIGNED_LONG(self._seq)
        cbc = modes.CBC(iv_seq)
        self._cipher = Cipher(self._aes, cbc)

    def encrypt(self, msg):
        """Encrypt the data and increment the sequence number."""
        self._seq += 1
        self._generate_cipher()

        if isinstance(msg, str):
            msg = msg.encode("utf-8")

        encryptor = self._cipher.encryptor()
        padder = padding.PKCS7(128).padder()
        padded_data = padder.update(msg) + padder.finalize()
        ciphertext = encryptor.update(padded_data) + encryptor.finalize()
        signature = hashlib.sha256(
            self._sig + PACK_SIGNED_LONG(self._seq) + ciphertext
        ).digest()
        return (signature + ciphertext, self._seq)

    def decrypt(self, msg):
        """Decrypt the data."""
        decryptor = self._cipher.decryptor()
        dp = decryptor.update(msg[32:]) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plaintextbytes = unpadder.update(dp) + unpadder.finalize()

        return plaintextbytes.decode()
