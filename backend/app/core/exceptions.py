class AppException(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class BadRequestException(AppException):
    pass


class UnauthorizedException(AppException):
    pass


class ForbiddenException(AppException):
    pass


class NotFoundException(AppException):
    pass


class TooManyRequestsException(AppException):
    pass


class ServiceUnavailableException(AppException):
    pass
