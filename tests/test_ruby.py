import unittest
from ebook_translate import core


class RubyTests(unittest.TestCase):
    def test_ruby_base_has_only_outer_markers_and_preserves_pronunciation(self):
        root = core.parse(b'<p xmlns="http://www.w3.org/1999/xhtml">A<ruby><rb><span class="upright">B</span></rb><rt>reading</rt></ruby>C</p>')
        source, _ = core.slots(root)
        self.assertEqual(source, 'A[[ET0001]]B[[ET0002]]C')
        unit = {'path': [], 'field': None, 'source': source}
        core.apply_translation(root, unit, '甲[[ET0001]]乙[[ET0002]]丙')
        self.assertEqual(root[0][1].text, 'reading')
        self.assertEqual(''.join(root[0][0].itertext()), '乙')
        self.assertEqual(root[0][0][0].get('class'), 'upright')


if __name__ == '__main__':
    unittest.main()
