class ProvInvalid(Exception):
    def __init__(self, message):
        super().__init__(message)


class ProvBadConfig(Exception):
    def __init__(self, message):
        super().__init__(message)
