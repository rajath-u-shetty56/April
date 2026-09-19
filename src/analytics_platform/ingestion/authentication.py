from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from analytics_platform.ingestion.credentials import authenticate_secret


class IngestionPrincipal:
    is_authenticated = True
    is_staff = False


class IngestionAuthentication(BaseAuthentication):
    def authenticate(self, request):
        header = get_authorization_header(request).split()
        if len(header) != 2 or header[0].lower() != b"bearer" or len(header[1]) > 128:
            raise AuthenticationFailed("Invalid ingestion credential.", code="INVALID_CREDENTIAL")
        try:
            secret = header[1].decode("ascii")
        except UnicodeDecodeError as error:
            raise AuthenticationFailed(
                "Invalid ingestion credential.", code="INVALID_CREDENTIAL"
            ) from error
        return IngestionPrincipal(), authenticate_secret(secret)

    def authenticate_header(self, request):
        return "Bearer"
