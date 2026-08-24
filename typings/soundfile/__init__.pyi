from types import TracebackType
from typing import BinaryIO
from typing import Literal
from typing import Self

from _typeshed import FileDescriptorOrPath

class SoundFileError(Exception): ...
class LibsndfileError(SoundFileError): ...

class _SoundFileInfo:
    format: str
    subtype: str
    samplerate: int
    channels: int
    frames: int

class SoundFile:
    format: str
    subtype: str
    samplerate: int
    channels: int
    frames: int

    def __init__(
        self,
        file: FileDescriptorOrPath | BinaryIO,
        mode: str = "r",
        samplerate: int = 0,
        channels: int = 0,
        subtype: str | None = None,
        endian: str | None = None,
        format: str | None = None,  # noqa: A002
        closefd: bool = True,
        compression_level: float | None = None,
        bitrate_mode: str | None = None,
    ) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]: ...
    def buffer_read(self, frames: int = -1, dtype: str = "float64") -> memoryview: ...
    def buffer_write(self, data: bytes, dtype: str) -> None: ...

def info(
    file: FileDescriptorOrPath | BinaryIO,
    verbose: bool = False,
) -> _SoundFileInfo: ...
