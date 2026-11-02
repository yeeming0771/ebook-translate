"""Create an original tiny Japanese EPUB for trying the Gemini web workflow."""

import argparse
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile


def create(output):
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        z.writestr("META-INF/container.xml", '''<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>''')
        z.writestr("package.opf", '''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">urn:uuid:86445697-4078-4895-8f87-30b1a30ef894</dc:identifier><dc:title>雨の配達</dc:title><dc:language>ja</dc:language><meta property="dcterms:modified">2026-11-02T00:00:00Z</meta></metadata><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="css" href="style.css" media-type="text/css"/></manifest><spine><itemref idref="chapter"/></spine></package>''')
        z.writestr("chapter.xhtml", '''<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ja"><head><title>雨の配達</title><link rel="stylesheet" href="style.css"/></head><body><h1 id="start">雨の配達</h1><p>朝、玄関に小さな箱が届いた。中には、昨日なくしたはずの<em>雨音</em>が入っていた。</p><p>「お届けが遅くなりました」と、配達員は晴れた空を見上げた。私は箱を開けないまま、お礼を言った。</p></body></html>''')
        z.writestr("nav.xhtml", '''<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ja"><head><title>目次</title></head><body><nav epub:type="toc"><h1>目次</h1><ol><li><a href="chapter.xhtml#start">雨の配達</a></li></ol></nav></body></html>''')
        z.writestr("style.css", "body{font-family:serif;line-height:1.8;margin:2em}p{text-indent:2em}")
    return output.resolve()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="work/demo.epub")
    args = parser.parse_args()
    print(create(args.output))
