use std::fs::File;
use std::io::{BufWriter, Cursor, Write};
use std::path::Path;

use image::imageops::FilterType;
use image::{ImageFormat, RgbaImage};
use pyo3::create_exception;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use rayon::prelude::*;
use squish::{Format, Params};

const DDS_MAGIC: &[u8; 4] = b"DDS ";
const DDS_HEADER_SIZE: u32 = 124;
const DDS_PIXEL_FORMAT_SIZE: u32 = 32;
const DDSD_CAPS: u32 = 0x1;
const DDSD_HEIGHT: u32 = 0x2;
const DDSD_WIDTH: u32 = 0x4;
const DDSD_PITCH: u32 = 0x8;
const DDSD_PIXELFORMAT: u32 = 0x1000;
const DDSD_MIPMAPCOUNT: u32 = 0x20000;
const DDSD_LINEARSIZE: u32 = 0x80000;
const DDSCAPS_COMPLEX: u32 = 0x8;
const DDSCAPS_TEXTURE: u32 = 0x1000;
const DDSCAPS_MIPMAP: u32 = 0x400000;
const DDPF_ALPHAPIXELS: u32 = 0x1;
const DDPF_FOURCC: u32 = 0x4;
const DDPF_RGB: u32 = 0x40;
const MIN_MIP_DIMENSION: u32 = 4;

create_exception!(_native, NativeAlphaMismatchError, PyValueError);

enum EncodeError {
    Message(String),
    AlphaMismatch {
        expected: String,
        actual: &'static str,
    },
}

impl From<String> for EncodeError {
    fn from(message: String) -> Self {
        Self::Message(message)
    }
}

#[derive(Clone, Copy)]
enum DdsFormat {
    Bgra,
    Bc1,
    Bc2,
    Bc3,
}

impl DdsFormat {
    fn parse(value: &str) -> Option<Self> {
        match value {
            "B8G8R8A8_UNORM" => Some(Self::Bgra),
            "BC1_UNORM" => Some(Self::Bc1),
            "BC2_UNORM" => Some(Self::Bc2),
            "BC3_UNORM" => Some(Self::Bc3),
            _ => None,
        }
    }

    fn squish(self) -> Option<Format> {
        match self {
            Self::Bgra => None,
            Self::Bc1 => Some(Format::Bc1),
            Self::Bc2 => Some(Format::Bc2),
            Self::Bc3 => Some(Format::Bc3),
        }
    }

    fn four_cc(self) -> [u8; 4] {
        match self {
            Self::Bgra => [0; 4],
            Self::Bc1 => *b"DXT1",
            Self::Bc2 => *b"DXT3",
            Self::Bc3 => *b"DXT5",
        }
    }
}

fn mip_chain(top: RgbaImage) -> Vec<RgbaImage> {
    let mut levels = vec![top];
    loop {
        let previous = levels.last().expect("mip chain is never empty");
        let width = (previous.width() / 2).max(1);
        let height = (previous.height() / 2).max(1);
        if width < MIN_MIP_DIMENSION || height < MIN_MIP_DIMENSION {
            break;
        }
        levels.push(image::imageops::resize(
            previous,
            width,
            height,
            FilterType::CatmullRom,
        ));
    }
    levels
}

fn write_u32(output: &mut impl Write, value: u32) -> std::io::Result<()> {
    output.write_all(&value.to_le_bytes())
}

fn encoded_level_size(format: DdsFormat, width: u32, height: u32) -> usize {
    match format {
        DdsFormat::Bgra => width as usize * height as usize * 4,
        DdsFormat::Bc1 => width.div_ceil(4) as usize * height.div_ceil(4) as usize * 8,
        DdsFormat::Bc2 | DdsFormat::Bc3 => {
            width.div_ceil(4) as usize * height.div_ceil(4) as usize * 16
        }
    }
}

fn write_dds(
    output: &mut impl Write,
    levels: &[RgbaImage],
    format: DdsFormat,
) -> Result<(), String> {
    let top = levels.first().ok_or("cannot encode an empty mip chain")?;
    output
        .write_all(DDS_MAGIC)
        .map_err(|error| error.to_string())?;
    write_u32(output, DDS_HEADER_SIZE).map_err(|error| error.to_string())?;
    let compressed = format.squish().is_some();
    let flags = DDSD_CAPS
        | DDSD_HEIGHT
        | DDSD_WIDTH
        | DDSD_PIXELFORMAT
        | DDSD_MIPMAPCOUNT
        | if compressed {
            DDSD_LINEARSIZE
        } else {
            DDSD_PITCH
        };
    write_u32(output, flags).map_err(|error| error.to_string())?;
    write_u32(output, top.height()).map_err(|error| error.to_string())?;
    write_u32(output, top.width()).map_err(|error| error.to_string())?;
    let pitch_or_size = if compressed {
        encoded_level_size(format, top.width(), top.height()) as u32
    } else {
        top.width() * 4
    };
    write_u32(output, pitch_or_size).map_err(|error| error.to_string())?;
    write_u32(output, 0).map_err(|error| error.to_string())?;
    write_u32(output, levels.len() as u32).map_err(|error| error.to_string())?;
    for _ in 0..11 {
        write_u32(output, 0).map_err(|error| error.to_string())?;
    }
    write_u32(output, DDS_PIXEL_FORMAT_SIZE).map_err(|error| error.to_string())?;
    if compressed {
        write_u32(output, DDPF_FOURCC).map_err(|error| error.to_string())?;
        output
            .write_all(&format.four_cc())
            .map_err(|error| error.to_string())?;
        for _ in 0..5 {
            write_u32(output, 0).map_err(|error| error.to_string())?;
        }
    } else {
        write_u32(output, DDPF_RGB | DDPF_ALPHAPIXELS).map_err(|error| error.to_string())?;
        write_u32(output, 0).map_err(|error| error.to_string())?;
        write_u32(output, 32).map_err(|error| error.to_string())?;
        write_u32(output, 0x00ff_0000).map_err(|error| error.to_string())?;
        write_u32(output, 0x0000_ff00).map_err(|error| error.to_string())?;
        write_u32(output, 0x0000_00ff).map_err(|error| error.to_string())?;
        write_u32(output, 0xff00_0000).map_err(|error| error.to_string())?;
    }
    write_u32(output, DDSCAPS_TEXTURE | DDSCAPS_COMPLEX | DDSCAPS_MIPMAP)
        .map_err(|error| error.to_string())?;
    for _ in 0..4 {
        write_u32(output, 0).map_err(|error| error.to_string())?;
    }

    for level in levels {
        if let Some(codec) = format.squish() {
            let mut encoded =
                vec![0; codec.compressed_size(level.width() as usize, level.height() as usize)];
            codec.compress(
                level.as_raw(),
                level.width() as usize,
                level.height() as usize,
                Params::default(),
                &mut encoded,
            );
            output
                .write_all(&encoded)
                .map_err(|error| error.to_string())?;
        } else {
            for pixel in level.pixels() {
                output
                    .write_all(&[pixel[2], pixel[1], pixel[0], pixel[3]])
                    .map_err(|error| error.to_string())?;
            }
        }
    }
    output.flush().map_err(|error| error.to_string())
}

fn write_tga(output: &mut impl Write, image: &RgbaImage) -> Result<(), String> {
    let width = u16::try_from(image.width()).map_err(|_| "TGA width exceeds 65535")?;
    let height = u16::try_from(image.height()).map_err(|_| "TGA height exceeds 65535")?;
    let mut header = [0u8; 18];
    header[2] = 2;
    header[12..14].copy_from_slice(&width.to_le_bytes());
    header[14..16].copy_from_slice(&height.to_le_bytes());
    header[16] = 32;
    header[17] = 0x28;
    output
        .write_all(&header)
        .map_err(|error| error.to_string())?;
    for pixel in image.pixels() {
        output
            .write_all(&[pixel[2], pixel[1], pixel[0], pixel[3]])
            .map_err(|error| error.to_string())?;
    }
    output
        .write_all(&[0; 8])
        .and_then(|()| output.write_all(b"TRUEVISION-XFILE.\0"))
        .and_then(|()| output.flush())
        .map_err(|error| error.to_string())
}

fn alpha_mode(image: &RgbaImage, has_alpha: bool) -> &'static str {
    if !has_alpha {
        return "none";
    }
    let alpha_mask = image
        .as_raw()
        .par_chunks_exact(4)
        .map(|pixel| match pixel[3] {
            0 => 0b001,
            255 => 0b010,
            _ => 0b100,
        })
        .reduce(|| 0, |left, right| left | right);
    match alpha_mask {
        0b001 => "transparent",
        0b010 | 0 => "opaque",
        0b011 => "binary",
        _ => "graded",
    }
}

fn alpha_is_compatible(expected: &str, actual: &str) -> bool {
    match expected {
        "none" | "opaque" => matches!(actual, "none" | "opaque"),
        "transparent" => actual == "transparent",
        "binary" => matches!(actual, "binary" | "graded"),
        "graded" => actual == "graded",
        _ => false,
    }
}

fn encode_bytes(
    source: &Path,
    format_name: &str,
    expected_alpha: Option<&str>,
) -> Result<Vec<u8>, EncodeError> {
    let decoded = image::open(source).map_err(|error| EncodeError::Message(error.to_string()))?;
    let has_alpha = decoded.color().has_alpha();
    let image = decoded.to_rgba8();
    let actual_alpha = alpha_mode(&image, has_alpha);
    if let Some(expected) = expected_alpha
        && !alpha_is_compatible(expected, actual_alpha)
    {
        return Err(EncodeError::AlphaMismatch {
            expected: expected.to_owned(),
            actual: actual_alpha,
        });
    }
    let mut output = Cursor::new(Vec::new());
    match DdsFormat::parse(format_name) {
        Some(format) => write_dds(&mut output, &mip_chain(image), format)?,
        None if format_name == "BMP" => image
            .write_to(&mut output, ImageFormat::Bmp)
            .map_err(|error| EncodeError::Message(error.to_string()))?,
        None if format_name == "TGA" => write_tga(&mut output, &image)?,
        None => {
            return Err(EncodeError::Message(format!(
                "unsupported native texture format: {format_name}"
            )));
        }
    };
    Ok(output.into_inner())
}

fn encode(
    source: &Path,
    destination: &Path,
    format_name: &str,
    expected_alpha: Option<&str>,
) -> Result<(), EncodeError> {
    let encoded = encode_bytes(source, format_name, expected_alpha)?;
    if let Some(parent) = destination.parent() {
        std::fs::create_dir_all(parent).map_err(|error| EncodeError::Message(error.to_string()))?;
    }
    let file =
        File::create(destination).map_err(|error| EncodeError::Message(error.to_string()))?;
    let mut output = BufWriter::new(file);
    output
        .write_all(&encoded)
        .and_then(|()| output.flush())
        .map_err(|error| EncodeError::Message(error.to_string()))
}

fn encode_error(error: EncodeError, source: &str) -> PyErr {
    match error {
        EncodeError::Message(message) => PyRuntimeError::new_err(message),
        EncodeError::AlphaMismatch { expected, actual } => NativeAlphaMismatchError::new_err(
            format!("Editable texture alpha changed from {expected} to {actual}: {source}"),
        ),
    }
}

#[pyfunction]
#[pyo3(signature = (source, destination, format_name, expected_alpha=None))]
fn encode_png(
    py: Python<'_>,
    source: &str,
    destination: &str,
    format_name: &str,
    expected_alpha: Option<&str>,
) -> PyResult<()> {
    let source = source.to_owned();
    let destination = destination.to_owned();
    let format_name = format_name.to_owned();
    let expected_alpha = expected_alpha.map(str::to_owned);
    py.detach(|| {
        encode(
            Path::new(&source),
            Path::new(&destination),
            &format_name,
            expected_alpha.as_deref(),
        )
    })
    .map_err(|error| encode_error(error, &source))
}

#[pyfunction]
#[pyo3(signature = (source, format_name, expected_alpha=None))]
fn encode_png_bytes(
    py: Python<'_>,
    source: &str,
    format_name: &str,
    expected_alpha: Option<&str>,
) -> PyResult<Py<PyBytes>> {
    let source = source.to_owned();
    let format_name = format_name.to_owned();
    let expected_alpha = expected_alpha.map(str::to_owned);
    let encoded = py
        .detach(|| encode_bytes(Path::new(&source), &format_name, expected_alpha.as_deref()))
        .map_err(|error| encode_error(error, &source))?;
    Ok(PyBytes::new(py, &encoded).unbind())
}

#[pyfunction]
fn supported_formats() -> Vec<&'static str> {
    vec![
        "B8G8R8A8_UNORM",
        "BC1_UNORM",
        "BC2_UNORM",
        "BC3_UNORM",
        "BMP",
        "TGA",
    ]
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(encode_png, module)?)?;
    module.add_function(wrap_pyfunction!(encode_png_bytes, module)?)?;
    module.add_function(wrap_pyfunction!(supported_formats, module)?)?;
    module.add("NativeEncoderError", module.py().get_type::<PyValueError>())?;
    module.add(
        "NativeAlphaMismatchError",
        module.py().get_type::<NativeAlphaMismatchError>(),
    )?;
    Ok(())
}
