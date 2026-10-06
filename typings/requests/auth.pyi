from . import PreparedRequest

class AuthBase:
    def __call__(self, request: PreparedRequest) -> PreparedRequest: ...
