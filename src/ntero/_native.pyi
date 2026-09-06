class NativeAlphaMismatchError(ValueError): ...

def encode_png(
    source: str,
    destination: str,
    format_name: str,
    expected_alpha: str | None = None,
    flip_vertical: bool = False,
) -> None: ...
def encode_png_bytes(
    source: str,
    format_name: str,
    expected_alpha: str | None = None,
    flip_vertical: bool = False,
) -> bytes: ...
def supported_formats() -> list[str]: ...
