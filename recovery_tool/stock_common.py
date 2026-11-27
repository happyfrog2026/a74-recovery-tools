"""Local validation errors shared by the offline stock SD planner."""


class StockRecoveryError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise StockRecoveryError(code, message)
