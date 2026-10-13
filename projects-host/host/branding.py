"""Response-only browser branding; saved Studio sources remain untouched."""
from html.parser import HTMLParser

# Bump the pathname version whenever the artwork or generated icon format changes.
ICON_PREFIX = 'mungoon-v3'
ICON_SIZES = (16, 32, 48, 192)
BRAND_ASSETS = {'/brand.png': 'brand.png'}
for _name in [ICON_PREFIX+'.ico', *(f'{ICON_PREFIX}-{n}.png' for n in (*ICON_SIZES, 180))]:
    BRAND_ASSETS['/icons/'+_name] = 'icons/'+_name
BRAND_ASSETS.update({
    '/favicon.ico': 'icons/'+ICON_PREFIX+'.ico',
    '/apple-touch-icon.png': f'icons/{ICON_PREFIX}-180.png',
    '/apple-touch-icon-precomposed.png': f'icons/{ICON_PREFIX}-180.png',
})
ICONS = f'<link rel="icon" type="image/x-icon" sizes="16x16 32x32 48x48" href="/icons/{ICON_PREFIX}.ico">'
ICONS += ''.join(f'<link rel="icon" type="image/png" sizes="{n}x{n}" href="/icons/{ICON_PREFIX}-{n}.png">' for n in ICON_SIZES)
ICONS += f'<link rel="apple-touch-icon" sizes="180x180" href="/icons/{ICON_PREFIX}-180.png">'


class _BrandingParser(HTMLParser):
    def __init__(self, page):
        super().__init__(convert_charrefs=False)
        self.offsets = [0]
        for line in page.split('\n'):
            self.offsets.append(self.offsets[-1]+len(line)+1)
        self.removals = []
        self.head_end = self.head_start = self.html_start = None

    def source_offset(self):
        line, column = self.getpos()
        return self.offsets[line-1]+column

    def handle_starttag(self, tag, attrs):
        start = self.source_offset()
        end = start+len(self.get_starttag_text())
        if tag == 'link':
            rel = dict(attrs).get('rel', '') or ''
            if set(rel.lower().split()) & {'icon', 'apple-touch-icon', 'apple-touch-icon-precomposed'}:
                self.removals.append((start, end))
        elif tag == 'head' and self.head_start is None:
            self.head_start = end
        elif tag == 'html' and self.html_start is None:
            self.html_start = end

    def handle_endtag(self, tag):
        if tag == 'head' and self.head_end is None:
            self.head_end = self.source_offset()


def branded_html(data):
    page = data.decode('utf-8-sig')
    parser = _BrandingParser(page)
    parser.feed(page)
    pos = next((p for p in (parser.head_end, parser.head_start, parser.html_start) if p is not None), 0)
    edits = [(start, end, '') for start, end in parser.removals] + [(pos, pos, ICONS)]
    for start, end, value in sorted(edits, reverse=True):
        page = page[:start]+value+page[end:]
    return page.encode('utf-8')
