"""Error classes shared by the CLI, the pipeline and v2l-remote (standard library only)."""


class UnusableInput(ValueError):
    """The video cannot be used: it does not open, or no face is tracked where one is needed."""
