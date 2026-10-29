# Formats

> **Audience**: users who want to choose the right format for their mangas.
>
> **Reference**: See [ADR-002: No CBR Support](../development/decisions/002-no-cbr.md)
> for the legal and licensing rationale behind the format list.

NexusDL can package a downloaded chapter in **six formats**:

| Format | Extension | Container | Best for |
|---|---|---|---|
| **FOLDER** | (none) | Directory | Re-processing, editing |
| **ZIP** | `.zip` | ZIP | Generic use |
| **CBZ** | `.cbz` | ZIP + `ComicInfo.xml` | **Most readers** |
| **CB7** | `.cb7` | 7z + `ComicInfo.xml` | **Smaller files** |
| **PDF** | `.pdf` | PDF | Universal viewers |
| **CBT** | `.cbt` | tar + `ComicInfo.xml` | Archival (rare) |

**Recommended**: **CBZ** — best compatibility, good compression.

```{warning}
**CBR is not supported.** RAR is proprietary and incompatible with
GPL-3.0. See [ADR-002](../development/decisions/002-no-cbr.md).
```

---

## Table of contents

1. [Choose a format](#choose-a-format)
2. [FOLDER](#folder)
3. [ZIP](#zip)
4. [CBZ](#cbz)
5. [CB7](#cb7)
6. [PDF](#pdf)
7. [CBT](#cbt)
8. [ComicInfo.xml](#comicinfoxml)
9. [Compatibility matrix](#compatibility-matrix)
10. [Size comparison](#size-comparison)
11. [Advanced options](#advanced-options)
12. [Troubleshooting](#troubleshooting)

---

## Choose a format

Use this decision tree:

```
Do you want the raw images, without an archive?
├── Yes → FOLDER
└── No
    │
    Do you want a single file for a comic reader?
    ├── Yes
    │   │
    │   Does your reader support CBZ? (most do)
    │   ├── Yes → CBZ
    │   └── No
    │       │
    │       Does your reader support PDF? (universal)
    │       └── Yes → PDF
    │
    └── No (you want small files)
        │
        Does your reader support CB7? (Komga, Kavita, YACReader)
        ├── Yes → CB7
        └── No → CBZ (fallback)
```

### Quick recommendations

| Use case | Format |
|---|---|
| **Mihon / Tachiyomi**, **Komga**, **Kavita**, **YACReader** | **CBZ** |
| **Calibre**, **Adobe Reader** | **PDF** |
| **Smallest file size**, modern readers | **CB7** |
| **Custom processing**, OCR, editing | **FOLDER** |
| **Archive storage**, no re-compression | **CBT** |
| **Generic use**, unknown reader | **ZIP** |

---

## FOLDER

Raw image files in a directory. No compression, no archive.

### Structure

```
One Piece/
└── Chapter 001/
    ├── 001.jpg
    ├── 002.jpg
    ├── 003.jpg
    └── ...
```

### Pros

- ✅ **Fastest** — no compression, no archive creation
- ✅ **Editable** — open in any image editor
- ✅ **Transparent** — see exactly what was downloaded
- ✅ **No metadata loss** — preserves original files
- ✅ **Best for OCR, translation, ML pipelines**

### Cons

- ❌ **Many files** — 50+ files per chapter
- ❌ **No metadata** — no `ComicInfo.xml`
- ❌ **Not readable by comic readers** — they expect `.cbz`/`.pdf`
- ❌ **Slow to copy** — small files, many inodes

### When to use

- You plan to **convert** or **repackage** later
- You run **OCR** or **translation** tools
- You want to **edit** images before reading
- You **debug** a failing download

### Example

```bash
nexusdl download "https://mangadex.org/title/..." \
  --format FOLDER \
  --dest ~/Manga
```

Output:

```
~/Manga/One Piece/Chapter 001/
├── 001.jpg
├── 002.jpg
└── 003.jpg
```

---

## ZIP

Standard ZIP archive. No metadata, no special extension.

### Structure

```
Chapter 001.zip
├── 001.jpg
├── 002.jpg
└── 003.jpg
```

### Pros

- ✅ **Universal** — every OS can open ZIP
- ✅ **Fast** — ZIP compression is fast
- ✅ **Compatible** — no special reader required

### Cons

- ❌ **No metadata** — comic readers won't recognize it as a manga
- ❌ **Not readable by ComicRack, Komga, Kavita** — they expect `.cbz`

### When to use

- You want an **archive** but not a manga-specific format
- You share files with **non-comic-reader** users
- You plan to **convert** to `.cbz` later (`mv file.zip file.cbz`)

### Example

```bash
nexusdl download "https://mangadex.org/title/..." \
  --format ZIP \
  --dest ~/Manga
```

### Note

A `.cbz` is **just a ZIP** with `ComicInfo.xml` inside and a `.cbz`
extension. You can rename `file.zip` → `file.cbz` manually; it will work
in most readers, but **without metadata**.

---

## CBZ

**Comic Book ZIP**. The most common format for manga/comics.

### Structure

```
Chapter 001.cbz  (= ZIP)
├── 001.jpg
├── 002.jpg
├── 003.jpg
└── ComicInfo.xml   ← metadata
```

### Pros

- ✅ **Best compatibility** — supported by all major readers
- ✅ **Metadata** — `ComicInfo.xml` with title, author, chapter, etc.
- ✅ **Good compression** — ZIP is decent for images
- ✅ **Readable by every OS** — it's just a ZIP
- ✅ **The de facto standard** for manga collections

### Cons

- ⚠️ **Larger than CB7** — ZIP is less efficient than 7z (typically +10–20%)
- ⚠️ **No native reader on Windows** — need Komga, Kavita, YACReader, etc.

### When to use

- **Default choice** for almost everything
- You use **Mihon**, **Komga**, **Kavita**, **YACReader**, **CDisplayEx**
- You share your library with others
- You want maximum compatibility

### Example

```bash
nexusdl download "https://mangadex.org/title/..." \
  --format CBZ \
  --dest ~/Manga
```

### Metadata

`ComicInfo.xml` inside the CBZ contains:

```xml
<?xml version="1.0" encoding="utf-8"?>
<ComicInfo>
  <Title>Romance Dawn</Title>
  <Series>One Piece</Series>
  <Number>1</Number>
  <Count>1100</Count>
  <Volume>1</Volume>
  <Writer>Oda Eiichiro</Writer>
  <Penciller>Oda Eiichiro</Penciller>
  <Summary>A story about pirates...</Summary>
  <Year>1997</Year>
  <Genre>Action, Adventure, Comedy</Genre>
  <LanguageISO>en</LanguageISO>
  <PageCount>53</PageCount>
  <BlackAndWhite>No</BlackAndWhite>
  <Manga>YesAndRightToLeft</Manga>
  <Web>https://mangadex.org/title/...</Web>
  <Notes>Downloaded by NexusDL v0.1.0</Notes>
</ComicInfo>
```

See [ComicInfo.xml](#comicinfoxml) below for all fields.

---

## CB7

**Comic Book 7z**. Same as CBZ but using 7-Zip compression.

### Structure

```
Chapter 001.cb7  (= 7z archive)
├── 001.jpg
├── 002.jpg
├── 003.jpg
└── ComicInfo.xml   ← metadata
```

### Pros

- ✅ **Smaller files** — 7z outperforms ZIP on images (typically −10–20%)
- ✅ **Same metadata** — `ComicInfo.xml`
- ✅ **Compatible** — Komga, Kavita, YACReader, Calibre

### Cons

- ⚠️ **Slightly slower** to create (7z is more CPU-intensive than ZIP)
- ⚠️ **Less universal** — some readers don't support `.cb7`
- ⚠️ **No native Windows reader** — needs third-party tool

### When to use

- You have **many mangas** and want to save disk space
- You use **Komga**, **Kavita**, or **YACReader** (all support CB7)
- You archive long-term
- File size matters (NAS, cloud backup)

### Example

```bash
nexusdl download "https://mangadex.org/title/..." \
  --format CB7 \
  --dest ~/Manga
```

### Performance

CB7 is **~15 % smaller** than CBZ for typical manga pages (JPEG images).
For PNG images (rare), the difference is larger (**~30 %**).

The trade-off: **~2× slower** to create than CBZ (7z compression is more
CPU-intensive).

---

## PDF

**Portable Document Format**. Each image becomes a PDF page.

### Structure

```
Chapter 001.pdf
├── Page 1 (= image 001.jpg embedded)
├── Page 2 (= image 002.jpg embedded)
└── Page 3 (= image 003.jpg embedded)
```

### Pros

- ✅ **Universal** — every device can open PDF
- ✅ **No special reader** — browsers, phones, tablets, TVs
- ✅ **Good for reading** on tablets (iPad, Kindle Fire)
- ✅ **Printable** — for physical reading

### Cons

- ❌ **Larger files** — PDF embeds images with overhead (~+5%)
- ❌ **No ComicInfo.xml** — metadata is embedded in PDF metadata (limited)
- ❌ **Slower to create** — requires image → PDF conversion
- ❌ **Not ideal for manga readers** — Komga, Kavita, Mihon prefer CBZ

### When to use

- You read on **tablets** without a manga reader app
- You want **printable** output
- You share with people who don't know what a CBZ is
- You archive to systems that only support PDF

### Example

```bash
nexusdl download "https://mangadex.org/title/..." \
  --format PDF \
  --dest ~/Manga
```

### Metadata

PDF metadata (embedded in PDF):

| Field | Value |
|---|---|
| `Title` | Chapter title |
| `Author` | Manga author |
| `Subject` | Manga title |
| `Keywords` | Genres |
| `Creator` | `NexusDL v0.1.0` |
| `Producer` | `img2pdf` |

### Limitations

- **No right-to-left reading** — PDF doesn't support manga RTL mode.
  Pages are shown left-to-right.
- **No manga-specific metadata** — `ComicInfo.xml` is XML, PDF is binary.
- **Image quality preserved** — NexusDL uses `img2pdf` (lossless).

---

## CBT

**Comic Book tar**. Same as CBZ but using tar (uncompressed or gzip).

### Structure

```
Chapter 001.cbt  (= tar archive)
├── 001.jpg
├── 002.jpg
├── 003.jpg
└── ComicInfo.xml   ← metadata
```

### Pros

- ✅ **Metadata** — `ComicInfo.xml` supported
- ✅ **Compatible** — Komga, YACReader
- ✅ **Fast** — tar is fast (no compression by default)

### Cons

- ❌ **Larger** — tar is uncompressed (unless `.tar.gz`, which NexusDL
  doesn't use for `.cbt`)
- ❌ **Rare** — very few readers support CBT
- ❌ **No advantage over CBZ**

### When to use

- **Almost never** — CBZ is better in every way
- Only if a specific tool requires CBT

### Example

```bash
nexusdl download "https://mangadex.org/title/..." \
  --format CBT \
  --dest ~/Manga
```

---

## ComicInfo.xml

The **standard metadata file** for CBZ, CB7, CBT. Recognized by all major
comic readers.

### Full schema

```xml
<?xml version="1.0" encoding="utf-8"?>
<ComicInfo>
  <!-- Required -->
  <Title>Romance Dawn</Title>
  <Series>One Piece</Series>
  <Number>1</Number>

  <!-- Recommended -->
  <Count>1100</Count>
  <Volume>1</Volume>
  <Writer>Oda Eiichiro</Writer>
  <Penciller>Oda Eiichiro</Penciller>
  <Summary>A story about pirates...</Summary>
  <Year>1997</Year>
  <Genre>Action, Adventure, Comedy</Genre>
  <LanguageISO>en</LanguageISO>
  <PageCount>53</PageCount>

  <!-- Optional -->
  <AlternateSeries></AlternateSeries>
  <AlternateNumber></AlternateNumber>
  <AlternateCount></AlternateCount>
  <Inker></Inker>
  <Colorist></Colorist>
  <Letterer></Letterer>
  <CoverArtist></CoverArtist>
  <Editor></Editor>
  <Publisher></Publisher>
  <Imprint></Imprint>
  <Characters></Characters>
  <Teams></Teams>
  <Locations></Locations>
  <ScanInformation>NexusDL v0.1.0</ScanInformation>
  <StoryArc></StoryArc>
  <SeriesGroup></SeriesGroup>
  <AgeRating>Unknown</AgeRating>
  <Pages>
    <Page Image="0" Type="FrontCover" />
    <Page Image="1" />
    <Page Image="2" />
  </Pages>

  <!-- Manga-specific -->
  <BlackAndWhite>No</BlackAndWhite>
  <Manga>YesAndRightToLeft</Manga>

  <!-- Source -->
  <Web>https://mangadex.org/title/...</Web>
  <Notes>Downloaded by NexusDL v0.1.0 on 2026-10-28</Notes>
</ComicInfo>
```

### Fields used by NexusDL

| Field | Source |
|---|---|
| `Title` | Chapter title |
| `Series` | Manga title |
| `Number` | Chapter number |
| `Count` | Total chapter count |
| `Volume` | Chapter volume |
| `Writer` | Manga author |
| `Penciller` | Manga artist |
| `Summary` | Manga description |
| `Year` | Manga year |
| `Genre` | Manga genres (comma-separated) |
| `LanguageISO` | Chapter language (`en`, `fr`, `ja`, ...) |
| `PageCount` | Number of pages |
| `BlackAndWhite` | `Yes` if manga, `No` otherwise |
| `Manga` | `YesAndRightToLeft` if manga, `No` otherwise |
| `Web` | Chapter URL |
| `Notes` | `Downloaded by NexusDL vX.Y.Z on YYYY-MM-DD` |

### Reader compatibility

| Reader | Reads ComicInfo.xml |
|---|---|
| **Komga** | ✅ Full support |
| **Kavita** | ✅ Full support |
| **YACReader** | ✅ Full support |
| **Mihon / Tachiyomi** | ✅ (via extensions) |
| **CDisplayEx** | ✅ |
| **Calibre** | ⚠️ Partial (via plugin) |

---

## Compatibility matrix

Which reader supports which format:

| Reader | FOLDER | ZIP | CBZ | CB7 | PDF | CBT | CBR |
|---|---|---|---|---|---|---|---|
| **Komga** | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ⚠️* |
| **Kavita** | ❌ | ❌ | ✅ | ✅ | ✅ | ❌ | ⚠️* |
| **YACReader** | ⚠️ | ⚠️ | ✅ | ✅ | ❌ | ✅ | ⚠️* |
| **Mihon / Tachiyomi** | ❌ | ❌ | ✅ | ⚠️ | ⚠️ | ❌ | ⚠️* |
| **CDisplayEx** | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ✅ |
| **Calibre** | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ | ⚠️* |
| **Adobe Reader** | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ |
| **Windows Explorer** | ✅ | ✅ | ⚠️ | ⚠️ | ✅ | ⚠️ | ⚠️ |
| **macOS Preview** | ✅ | ✅ | ⚠️ | ⚠️ | ✅ | ⚠️ | ⚠️ |

⚠️ = supported with third-party plugin/tool (e.g. `unrar` for CBR).

**\*CBR requires `unrar`**, a proprietary binary. NexusDL **does not**
support CBR — see [ADR-002](../development/decisions/002-no-cbr.md).

---

## Size comparison

Typical manga chapter (50 pages, JPEG ~300 KB each = ~15 MB raw):

| Format | Size | Ratio | Speed |
|---|---|---|---|
| **FOLDER** | 15.0 MB | 100 % | Fastest |
| **ZIP** | 14.6 MB | 97 % | Fast |
| **CBZ** | 14.7 MB (+ metadata) | 98 % | Fast |
| **CB7** | 12.9 MB | 86 % | ~2× slower |
| **PDF** | 15.3 MB | 102 % | Slower |
| **CBT** | 15.1 MB | 101 % | Fast |

### Real-world example

For a **1100-chapter manga** (like One Piece):

| Format | Total size |
|---|---|
| **CBZ** | ~16.2 GB |
| **CB7** | ~14.2 GB (saves ~2 GB) |
| **PDF** | ~16.8 GB |

**CB7 saves 10–15 %** on large libraries, at the cost of slightly slower
packaging.

---

## Advanced options

### `--format` per download

```bash
nexusdl download "https://..." --format CBZ
nexusdl download "https://..." --format CB7
nexusdl download "https://..." --format PDF
```

### `--format` per chapter

```bash
nexusdl download "https://..." --chapters 1,2,3 --format PDF
```

### Metadata override

`ComicInfo.xml` is generated from `Manga` and `Chapter` metadata. If the
source metadata is incomplete, you can override:

```bash
nexusdl download "https://..." \
  --format CBZ \
  --metadata-title "My Custom Title" \
  --metadata-author "Unknown"
```

**Note**: `--metadata-*` flags are planned for v0.2.0.

### Compression level

For CB7, you can set the compression level (1 = fastest, 9 = smallest):

```bash
nexusdl download "https://..." --format CB7 --compression 7
```

**Note**: `--compression` flag is planned for v0.2.0.

### Image quality

By default, NexusDL **re-encodes nothing** — the original images are
packed as-is.

To re-encode (e.g. JPEG → WebP, smaller files):

```bash
nexusdl download "https://..." \
  --format CBZ \
  --image-format webp \
  --image-quality 85
```

**Note**: `--image-format` and `--image-quality` flags are planned for
v0.2.0.

### Format conversion

Convert existing `.cbz` to `.cb7`:

```bash
nexusdl convert ~/Manga/One\ Piece/Chapter\ 001.cbz --to CB7
```

**Note**: `nexusdl convert` is planned for v0.2.0.

---

## Troubleshooting

### CBZ won't open in my reader

**Cause**: the file is a ZIP renamed to `.cbz` but without `ComicInfo.xml`.

**Fix**: check the file:

```bash
unzip -l "Chapter 001.cbz"
```

If you see only `.jpg` files (no `ComicInfo.xml`), the file is a ZIP
renamed manually. NexusDL **always** generates `ComicInfo.xml`, so this
happens only if you renamed it yourself.

### PDF shows wrong page order

**Cause**: manga is **right-to-left**, but PDF is **left-to-right**.

**Fix**: PDF doesn't support manga RTL. Use CBZ with a manga reader that
respects `Manga=YesAndRightToLeft` (Komga, Kavita, YACReader).

### CB7 is slower than CBZ

**Cause**: 7z is more CPU-intensive than ZIP.

**Fix**: if speed matters more than size, use CBZ. If size matters, keep
CB7.

### `unrar` error

**Cause**: NexusDL does **not** support CBR. If you see a `unrar` error,
it comes from an **external plugin**, not NexusDL core.

**Fix**: see [ADR-002](../development/decisions/002-no-cbr.md). CBR is
not supported by design.

### `img2pdf` fails on PDF

**Cause**: an image is corrupted or in an unsupported format.

**Fix**: try re-downloading the chapter. If the image is still broken,
the source site is at fault. Open an issue with the URL.

### Chapter is a `.zip` on disk but has `.cbz` extension

**Cause**: you renamed it manually.

**Fix**: run `unzip -l` to check the contents. If it's a valid CBZ, it
should include `ComicInfo.xml`. If not, re-download with NexusDL (which
generates the metadata).

### `Permission denied` on output

**Cause**: the download directory is not writable.

**Fix**:

```bash
chmod u+w ~/Manga
```

### Format not recognized by Mihon

**Cause**: Mihon expects CBZ in a specific structure.

**Fix**: NexusDL generates the standard structure (`001.jpg`, `002.jpg`,
`ComicInfo.xml`). If Mihon doesn't recognize it, ensure:

- The file extension is `.cbz` (lowercase).
- `ComicInfo.xml` is present and valid.
- The images are named `001.jpg`, `002.jpg`, etc. (zero-padded to 3 digits).

---

## See also

- [ADR-002: No CBR Support](../development/decisions/002-no-cbr.md)
- [CLI usage](cli.md) — `nexusdl download --format`
- [Configuration](configuration.md) — env vars
- [Docker installation](../installation/docker.md) — format in Docker
- [ComicInfo.xml schema](https://github.com/anansi-project/comicinfo) — official spec
- [Komga](https://komga.org/) — server supporting CBZ, CB7, PDF, CBT
- [Kavita](https://www.kavitareader.com/) — server supporting CBZ, CB7, PDF
- [YACReader](https://www.yacreader.com/) — reader supporting CBZ, CB7, CBT

---

*This page is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../LICENSE).*
