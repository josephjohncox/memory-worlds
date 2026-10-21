class MemoryError(Exception):
    status = 400
    code = "invalid_request"


class Unauthorized(MemoryError):
    status = 401
    code = "unauthorized"


class Forbidden(MemoryError):
    status = 403
    code = "forbidden"


class NotFound(MemoryError):
    status = 404
    code = "not_found"


class Conflict(MemoryError):
    status = 409
    code = "conflict"


class Unavailable(MemoryError):
    status = 503
    code = "unavailable"
