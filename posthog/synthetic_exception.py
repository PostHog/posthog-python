class SyntheticException(BaseException):
    """A synthetic exception that wraps exc_info tuples for proper exception handling."""

    def __init__(self, exc_type, exc_value, exc_traceback):
        self.exc_type = exc_type
        self.exc_value = exc_value
        self.exc_traceback = exc_traceback
        self.__traceback__ = exc_traceback

        # Set the exception message
        if exc_value:
            super().__init__(str(exc_value))
        else:
            super().__init__(f"{exc_type.__name__}")
