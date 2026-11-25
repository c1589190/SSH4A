class AppError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind

    def status(self) -> dict[str, str]:
        return {"kind": "client_error", "error": self.kind, "message": str(self)}
