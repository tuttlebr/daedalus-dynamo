use std::io::Cursor;

use image::codecs::jpeg::JpegEncoder;
use image::imageops::FilterType;
use image::{DynamicImage, ImageBuffer, ImageFormat, ImageReader, Rgb};
use libheif_rs::{ColorSpace, HeifContext, LibHeif, RgbChroma};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LlmContentType {
    Jpeg,
    Png,
}

impl LlmContentType {
    pub fn mime(self) -> &'static str {
        match self {
            LlmContentType::Jpeg => "image/jpeg",
            LlmContentType::Png => "image/png",
        }
    }
}

#[derive(Debug, Clone)]
pub struct PreparedImage {
    pub bytes: Vec<u8>,
    pub content_type: LlmContentType,
    pub width: u32,
    pub height: u32,
    pub original_mime: String,
    pub transcoded: bool,
}

/// Detect the input format and return a tuple of (ImageFormat, original_mime).
pub fn detect_format(bytes: &[u8]) -> Result<(ImageFormat, String), String> {
    let fmt =
        image::guess_format(bytes).map_err(|e| format!("Failed to detect image format: {e}"))?;
    let mime = match fmt {
        ImageFormat::Jpeg => "image/jpeg",
        ImageFormat::Png => "image/png",
        ImageFormat::WebP => "image/webp",
        ImageFormat::Gif => "image/gif",
        ImageFormat::Bmp => "image/bmp",
        ImageFormat::Tiff => "image/tiff",
        other => {
            return Err(format!("Unsupported image format: {other:?}"));
        }
    }
    .to_string();
    Ok((fmt, mime))
}

/// Detect HEIC/HEIF via the ISOBMFF `ftyp` box brand at offset 8.
fn heif_mime(bytes: &[u8]) -> Option<&'static str> {
    if bytes.len() < 12 || &bytes[4..8] != b"ftyp" {
        return None;
    }
    match &bytes[8..12] {
        b"heic" | b"heix" | b"heim" | b"heis" | b"hevc" | b"hevm" | b"hevs" => Some("image/heic"),
        b"mif1" | b"msf1" => Some("image/heif"),
        _ => None,
    }
}

/// Prepare an image for the LLM. PNG/JPEG pass through unchanged unless they
/// exceed `max_edge` and `downscale_large` is true. Other formats are decoded,
/// optionally downscaled, and re-encoded as JPEG at `quality`.
pub fn prepare_for_llm(
    bytes: &[u8],
    max_edge: u32,
    downscale_large: bool,
    jpeg_quality: u8,
    max_pixels: u64,
) -> Result<PreparedImage, String> {
    if let Some(mime) = heif_mime(bytes) {
        let img = decode_heif(bytes, max_pixels)?;
        return finalize_transcode(
            img,
            mime.to_string(),
            max_edge,
            downscale_large,
            jpeg_quality,
            max_pixels,
        );
    }

    let (fmt, original_mime) = detect_format(bytes)?;

    // Try to determine the on-disk dimensions cheaply for PNG/JPEG without a full decode.
    let dims_cheap = ImageReader::new(Cursor::new(bytes))
        .with_guessed_format()
        .ok()
        .and_then(|r| r.into_dimensions().ok());

    let needs_transcode = !matches!(fmt, ImageFormat::Jpeg | ImageFormat::Png);
    let oversized = match dims_cheap {
        Some((w, h)) => {
            ensure_pixel_count(w, h, max_pixels)?;
            downscale_large && w.max(h) > max_edge
        }
        None => false,
    };

    if !needs_transcode && !oversized {
        let (w, h) = dims_cheap.ok_or_else(|| "Failed to read dimensions".to_string())?;
        let content_type = match fmt {
            ImageFormat::Png => LlmContentType::Png,
            _ => LlmContentType::Jpeg,
        };
        return Ok(PreparedImage {
            bytes: bytes.to_vec(),
            content_type,
            width: w,
            height: h,
            original_mime,
            transcoded: false,
        });
    }

    // Decode (handles GIF/TIFF/BMP/WebP first frame too)
    let img = image::load_from_memory(bytes).map_err(|e| format!("Failed to decode image: {e}"))?;

    finalize_transcode(
        img,
        original_mime,
        max_edge,
        downscale_large,
        jpeg_quality,
        max_pixels,
    )
}

/// Downscale the decoded image if needed and re-encode as JPEG.
fn finalize_transcode(
    img: DynamicImage,
    original_mime: String,
    max_edge: u32,
    downscale_large: bool,
    jpeg_quality: u8,
    max_pixels: u64,
) -> Result<PreparedImage, String> {
    let (w, h) = (img.width(), img.height());
    ensure_pixel_count(w, h, max_pixels)?;
    let downscaled = if downscale_large && w.max(h) > max_edge {
        let scale = max_edge as f32 / w.max(h) as f32;
        let nw = (w as f32 * scale).round().max(1.0) as u32;
        let nh = (h as f32 * scale).round().max(1.0) as u32;
        img.resize(nw, nh, FilterType::Lanczos3)
    } else {
        img
    };

    let (final_w, final_h) = (downscaled.width(), downscaled.height());

    let mut out = Vec::with_capacity(2 * 1024 * 1024);
    encode_jpeg(&downscaled, &mut out, jpeg_quality)
        .map_err(|e| format!("Failed to encode JPEG: {e}"))?;

    Ok(PreparedImage {
        bytes: out,
        content_type: LlmContentType::Jpeg,
        width: final_w,
        height: final_h,
        original_mime,
        transcoded: true,
    })
}

/// Decode any service-supported image into a DynamicImage, including HEIC/HEIF.
pub fn decode_supported_image(bytes: &[u8], max_pixels: u64) -> Result<DynamicImage, String> {
    if heif_mime(bytes).is_some() {
        return decode_heif(bytes, max_pixels);
    }
    if let Some((w, h)) = ImageReader::new(Cursor::new(bytes))
        .with_guessed_format()
        .ok()
        .and_then(|reader| reader.into_dimensions().ok())
    {
        ensure_pixel_count(w, h, max_pixels)?;
    }
    let img = image::load_from_memory(bytes).map_err(|e| format!("decode: {e}"))?;
    ensure_pixel_count(img.width(), img.height(), max_pixels)?;
    Ok(img)
}

/// Decode a HEIC/HEIF buffer into a `DynamicImage` via libheif. Orientation
/// transforms in the file are applied during decode (libheif default).
fn decode_heif(bytes: &[u8], max_pixels: u64) -> Result<DynamicImage, String> {
    let lib = LibHeif::new();
    let ctx = HeifContext::read_from_bytes(bytes)
        .map_err(|e| format!("HEIC: read context failed: {e}"))?;
    let handle = ctx
        .primary_image_handle()
        .map_err(|e| format!("HEIC: no primary image handle: {e}"))?;
    ensure_pixel_count(handle.width(), handle.height(), max_pixels)?;
    let decoded = lib
        .decode(&handle, ColorSpace::Rgb(RgbChroma::Rgb), None)
        .map_err(|e| format!("HEIC: decode failed: {e}"))?;

    let width = decoded.width();
    let height = decoded.height();
    let planes = decoded.planes();
    let plane = planes
        .interleaved
        .ok_or_else(|| "HEIC: no interleaved RGB plane".to_string())?;
    let stride = plane.stride;
    let row_bytes = (width as usize) * 3;
    if stride < row_bytes {
        return Err(format!("HEIC: stride {stride} < row {row_bytes}"));
    }

    let mut packed = vec![0u8; row_bytes * height as usize];
    for y in 0..height as usize {
        let src_off = y * stride;
        let dst_off = y * row_bytes;
        packed[dst_off..dst_off + row_bytes]
            .copy_from_slice(&plane.data[src_off..src_off + row_bytes]);
    }

    let buf: ImageBuffer<Rgb<u8>, Vec<u8>> = ImageBuffer::from_raw(width, height, packed)
        .ok_or_else(|| "HEIC: failed to wrap RGB buffer".to_string())?;
    Ok(DynamicImage::ImageRgb8(buf))
}

pub fn ensure_pixel_count(width: u32, height: u32, max_pixels: u64) -> Result<(), String> {
    let pixels = u64::from(width) * u64::from(height);
    if pixels > max_pixels {
        return Err(format!(
            "image dimensions {width}x{height} exceed MAX_IMAGE_PIXELS={max_pixels}"
        ));
    }
    Ok(())
}

fn encode_jpeg(img: &DynamicImage, out: &mut Vec<u8>, quality: u8) -> image::ImageResult<()> {
    // JPEG doesn't support alpha — convert RGBA to RGB
    let rgb = img.to_rgb8();
    let mut encoder = JpegEncoder::new_with_quality(out, quality);
    encoder.encode(
        rgb.as_raw(),
        rgb.width(),
        rgb.height(),
        image::ExtendedColorType::Rgb8,
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::{ImageBuffer, Rgb};

    fn synth_jpeg(w: u32, h: u32) -> Vec<u8> {
        let img: ImageBuffer<Rgb<u8>, Vec<u8>> = ImageBuffer::from_fn(w, h, |x, y| {
            Rgb([(x % 256) as u8, (y % 256) as u8, ((x + y) % 256) as u8])
        });
        let mut buf = Vec::new();
        let mut enc = JpegEncoder::new_with_quality(&mut buf, 85);
        enc.encode(img.as_raw(), w, h, image::ExtendedColorType::Rgb8)
            .unwrap();
        buf
    }

    fn synth_png(w: u32, h: u32) -> Vec<u8> {
        let img: ImageBuffer<Rgb<u8>, Vec<u8>> = ImageBuffer::from_fn(w, h, |x, y| {
            Rgb([(x % 256) as u8, (y % 256) as u8, ((x + y) % 256) as u8])
        });
        let dyn_img = DynamicImage::ImageRgb8(img);
        let mut buf = Vec::new();
        dyn_img
            .write_to(&mut Cursor::new(&mut buf), ImageFormat::Png)
            .unwrap();
        buf
    }

    #[test]
    fn jpeg_passthrough_when_small() {
        let bytes = synth_jpeg(640, 480);
        let prep = prepare_for_llm(&bytes, 2048, true, 85, 80_000_000).unwrap();
        assert!(!prep.transcoded);
        assert_eq!(prep.content_type, LlmContentType::Jpeg);
        assert_eq!(prep.original_mime, "image/jpeg");
        assert_eq!(prep.bytes, bytes);
    }

    #[test]
    fn png_passthrough_when_small() {
        let bytes = synth_png(640, 480);
        let prep = prepare_for_llm(&bytes, 2048, true, 85, 80_000_000).unwrap();
        assert!(!prep.transcoded);
        assert_eq!(prep.content_type, LlmContentType::Png);
        assert_eq!(prep.original_mime, "image/png");
        assert_eq!(prep.bytes, bytes);
    }

    #[test]
    fn jpeg_downscaled_when_large() {
        let bytes = synth_jpeg(4096, 2048);
        let prep = prepare_for_llm(&bytes, 2048, true, 85, 80_000_000).unwrap();
        assert!(prep.transcoded);
        assert_eq!(prep.content_type, LlmContentType::Jpeg);
        assert!(prep.width.max(prep.height) <= 2048);
    }

    #[test]
    fn detect_jpeg() {
        let bytes = synth_jpeg(32, 32);
        let (fmt, mime) = detect_format(&bytes).unwrap();
        assert_eq!(fmt, ImageFormat::Jpeg);
        assert_eq!(mime, "image/jpeg");
    }

    #[test]
    fn detects_heic_and_heif_brands() {
        let mut heic = vec![0; 12];
        heic[4..8].copy_from_slice(b"ftyp");
        heic[8..12].copy_from_slice(b"heic");
        assert_eq!(heif_mime(&heic), Some("image/heic"));

        let mut heif = heic.clone();
        heif[8..12].copy_from_slice(b"mif1");
        assert_eq!(heif_mime(&heif), Some("image/heif"));
    }
}
