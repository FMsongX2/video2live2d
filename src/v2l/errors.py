"""Error classes shared by the CLI, the pipeline and v2l-remote (standard library only)."""


class UnusableInput(ValueError):
    """The input cannot be used: the video does not open, no face is tracked where one is needed,
    or the audio is missing, undecodable or silent."""


class AudioUnavailable(RuntimeError):
    """Audio2Face-3D cannot run here (the `audio` extra, the model files or a device is missing)."""
