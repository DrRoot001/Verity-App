//! OCR for resumes that are pictures of text: photos, screenshots and scanned
//! PDFs. It runs on the OS's own engine (Vision on macOS, Windows.Media.Ocr on
//! Windows), so it works offline, costs nothing, and only the extracted text
//! ever reaches the AI provider.

/// Scanned resumes are rarely longer than this. It bounds the import time.
const MAX_PAGES: usize = 10;
/// Renders PDF pages at 144 dpi, sharp enough for 9–10 pt resume type.
const RENDER_SCALE: f64 = 2.0;

pub fn image_text(bytes: &[u8]) -> Result<String, String> {
    imp::image_text(bytes)
}

pub fn pdf_text(bytes: &[u8]) -> Result<String, String> {
    imp::pdf_text(bytes)
}

#[cfg(target_os = "macos")]
mod imp {
    use super::{MAX_PAGES, RENDER_SCALE};
    use objc2::{rc::Retained, AnyThread};
    use objc2_core_foundation::{CFData, CFRetained, CGPoint, CGRect, CGSize};
    use objc2_core_graphics::{
        CGBitmapContextCreateImage, CGColorSpace, CGContext, CGDataProvider, CGImageAlphaInfo,
        CGPDFBox, CGPDFDocument, CGPDFPage,
    };
    use objc2_foundation::{NSArray, NSData, NSDictionary};
    use objc2_vision::{
        VNImageRequestHandler, VNRecognizeTextRequest, VNRequest, VNRequestTextRecognitionLevel,
    };
    use std::{ffi::c_void, ptr::NonNull};

    // objc2-core-graphics leaves this one unbound.
    extern "C-unwind" {
        fn CGBitmapContextCreate(
            data: *mut c_void,
            width: usize,
            height: usize,
            bits_per_component: usize,
            bytes_per_row: usize,
            space: Option<&CGColorSpace>,
            bitmap_info: u32,
        ) -> Option<NonNull<CGContext>>;
    }

    fn recognize(handler: &VNImageRequestHandler) -> Result<String, String> {
        let request = VNRecognizeTextRequest::new();
        request.setRecognitionLevel(VNRequestTextRecognitionLevel::Accurate);
        request.setUsesLanguageCorrection(true);
        let as_request: Retained<VNRequest> = request.clone().into_super().into_super();
        handler
            .performRequests_error(&NSArray::from_retained_slice(&[as_request]))
            .map_err(|error| {
                format!("Text recognition failed: {}", error.localizedDescription())
            })?;
        let lines: Vec<String> = request
            .results()
            .map(|results| {
                results
                    .iter()
                    .filter_map(|line| line.topCandidates(1).firstObject())
                    .map(|text| text.string().to_string())
                    .collect()
            })
            .unwrap_or_default();
        Ok(lines.join("\n"))
    }

    pub fn image_text(bytes: &[u8]) -> Result<String, String> {
        let handler = VNImageRequestHandler::initWithData_options(
            VNImageRequestHandler::alloc(),
            &NSData::with_bytes(bytes),
            &NSDictionary::new(),
        );
        recognize(&handler)
    }

    pub fn pdf_text(bytes: &[u8]) -> Result<String, String> {
        let unreadable = || "The PDF could not be opened.".to_string();
        let data = CFData::from_bytes(bytes);
        let provider = CGDataProvider::with_cf_data(Some(&data)).ok_or_else(unreadable)?;
        let document = CGPDFDocument::with_provider(Some(&provider)).ok_or_else(unreadable)?;
        let space = CGColorSpace::new_device_rgb().ok_or_else(unreadable)?;
        let page_count = CGPDFDocument::number_of_pages(Some(&document)).min(MAX_PAGES);
        let mut pages = Vec::new();
        for number in 1..=page_count {
            let Some(page) = CGPDFDocument::page(Some(&document), number) else {
                continue;
            };
            let bounds = CGPDFPage::box_rect(Some(&page), CGPDFBox::MediaBox);
            let width = (bounds.size.width * RENDER_SCALE) as usize;
            let height = (bounds.size.height * RENDER_SCALE) as usize;
            // SAFETY: a null data pointer and 0 bytes-per-row let CoreGraphics
            // allocate and own the pixel buffer; the returned context is +1.
            let context = unsafe {
                CGBitmapContextCreate(
                    std::ptr::null_mut(),
                    width,
                    height,
                    8,
                    0,
                    Some(&space),
                    CGImageAlphaInfo::PremultipliedLast.0,
                )
            }
            .map(|raw| unsafe { CFRetained::from_raw(raw) })
            .ok_or_else(unreadable)?;
            // Scans are often transparent outside the ink; OCR wants paper.
            CGContext::set_rgb_fill_color(Some(&context), 1.0, 1.0, 1.0, 1.0);
            CGContext::fill_rect(
                Some(&context),
                CGRect::new(CGPoint::ZERO, CGSize::new(width as f64, height as f64)),
            );
            CGContext::scale_ctm(Some(&context), RENDER_SCALE, RENDER_SCALE);
            CGContext::translate_ctm(Some(&context), -bounds.origin.x, -bounds.origin.y);
            CGContext::draw_pdf_page(Some(&context), Some(&page));
            let image = CGBitmapContextCreateImage(Some(&context)).ok_or_else(unreadable)?;
            // SAFETY: an empty options dictionary is always valid.
            let handler = unsafe {
                VNImageRequestHandler::initWithCGImage_options(
                    VNImageRequestHandler::alloc(),
                    &image,
                    &NSDictionary::new(),
                )
            };
            pages.push(recognize(&handler)?);
        }
        Ok(pages.join("\n\n"))
    }
}

#[cfg(windows)]
mod imp {
    use super::{MAX_PAGES, RENDER_SCALE};
    use windows::{
        core::Interface,
        Data::Pdf::{PdfDocument, PdfPageRenderOptions},
        Graphics::Imaging::BitmapDecoder,
        Media::Ocr::OcrEngine,
        Storage::Streams::{DataWriter, IRandomAccessStream, InMemoryRandomAccessStream},
    };

    fn message(error: windows::core::Error) -> String {
        format!("Text recognition failed: {}", error.message())
    }

    fn stream_of(bytes: &[u8]) -> windows::core::Result<IRandomAccessStream> {
        let stream = InMemoryRandomAccessStream::new()?;
        let writer = DataWriter::CreateDataWriter(&stream)?;
        writer.WriteBytes(bytes)?;
        writer.StoreAsync()?.get()?;
        writer.DetachStream()?;
        stream.Seek(0)?;
        stream.cast()
    }

    fn recognize(engine: &OcrEngine, image: &IRandomAccessStream) -> windows::core::Result<String> {
        let bitmap = BitmapDecoder::CreateAsync(image)?
            .get()?
            .GetSoftwareBitmapAsync()?
            .get()?;
        let result = engine.RecognizeAsync(&bitmap)?.get()?;
        let mut lines = Vec::new();
        for line in result.Lines()? {
            lines.push(line.Text()?.to_string());
        }
        Ok(lines.join("\n"))
    }

    fn engine() -> Result<OcrEngine, String> {
        OcrEngine::TryCreateFromUserProfileLanguages().map_err(|_| {
            "Windows has no OCR language installed. Add English under Settings → Time & language."
                .to_string()
        })
    }

    pub fn image_text(bytes: &[u8]) -> Result<String, String> {
        let engine = engine()?;
        recognize(&engine, &stream_of(bytes).map_err(message)?).map_err(message)
    }

    pub fn pdf_text(bytes: &[u8]) -> Result<String, String> {
        let engine = engine()?;
        let run = || -> windows::core::Result<String> {
            let document = PdfDocument::LoadFromStreamAsync(&stream_of(bytes)?)?.get()?;
            let max_side = OcrEngine::MaxImageDimension()? as f64;
            let mut pages = Vec::new();
            for index in 0..document.PageCount()?.min(MAX_PAGES as u32) {
                let page = document.GetPage(index)?;
                let size = page.Size()?;
                let longest = size.Width.max(size.Height).max(1.0) as f64;
                let scale = RENDER_SCALE.min(max_side / longest);
                let options = PdfPageRenderOptions::new()?;
                options.SetDestinationWidth((size.Width as f64 * scale) as u32)?;
                options.SetDestinationHeight((size.Height as f64 * scale) as u32)?;
                let image = InMemoryRandomAccessStream::new()?;
                page.RenderWithOptionsToStreamAsync(&image, &options)?
                    .get()?;
                pages.push(recognize(&engine, &image.cast()?)?);
            }
            Ok(pages.join("\n\n"))
        };
        run().map_err(message)
    }
}

#[cfg(not(any(target_os = "macos", windows)))]
mod imp {
    pub fn image_text(_: &[u8]) -> Result<String, String> {
        Err("OCR needs macOS or Windows.".to_string())
    }
    pub fn pdf_text(_: &[u8]) -> Result<String, String> {
        Err("OCR needs macOS or Windows.".to_string())
    }
}

#[cfg(all(test, any(target_os = "macos", windows)))]
mod tests {
    // Both fixtures hold only pixels, no text layer: a PNG and a PDF that
    // wraps that same image, exactly what a phone scan produces.
    const PNG: &[u8] = include_bytes!("../tests/fixtures/scanned-resume.png");
    const PDF: &[u8] = include_bytes!("../tests/fixtures/scanned-resume.pdf");

    fn assert_reads_resume(text: &str) {
        let text = text.to_lowercase();
        assert!(text.contains("jordan rivera"), "got: {text}");
        assert!(text.contains("kubernetes"), "got: {text}");
    }

    #[test]
    fn reads_text_from_an_image() {
        assert_reads_resume(&super::image_text(PNG).unwrap());
    }

    #[test]
    fn reads_text_from_a_scanned_pdf() {
        // The fixture really has no text layer, so OCR is what's being tested.
        assert!(pdf_extract::extract_text_from_mem(PDF)
            .unwrap_or_default()
            .trim()
            .is_empty());
        assert_reads_resume(&super::pdf_text(PDF).unwrap());
    }
}
