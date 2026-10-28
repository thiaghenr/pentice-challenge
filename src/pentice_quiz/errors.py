class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class InvalidRequestError(AppError):
    status_code = 422
    code = "invalid_request"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ImmutableRecordError(ConflictError):
    code = "immutable_record"


class SourceUnavailableError(AppError):
    status_code = 502
    code = "source_unavailable"


class GenerationUnavailableError(AppError):
    status_code = 503
    code = "generation_unavailable"
