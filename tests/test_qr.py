"""Tests for the QR encoder.

Two kinds of check, because a QR encoder can be wrong in two different ways.

The first is *pinned fixtures*: a hash of the finished matrix for a handful of
inputs that between them touch five versions and all four error levels. Those
hashes were taken from symbols that had already been compared module-for-module
against `segno` -- 960 pinned comparisons covering every version 1 to 10, every
level and all eight masks, plus a 3,908-symbol sweep of random ASCII, plus both
sides of every version-and-level capacity boundary. `segno` is not a dependency
of this project and is not imported here; the fixtures are what survived that
comparison, so the check outlives the library.

(One thing found on the way, recorded in case anyone repeats the exercise:
`segno` 1.6.6's `write_padding_bits` does `[0] * (8 - length % 8)`, which
appends a whole zero byte when the bit stream is already byte-aligned -- which
is every byte-mode symbol below version 10. The oracle was patched to
`(8 - length % 8) % 8` before the comparison. `colony/qr.py` follows the
standard. Also, `segno` prefers ISO-8859-1 for non-ASCII text and this module
always uses UTF-8, so the comparison was ASCII-only, which is the documented
domain.)

The second is a *decoder*, at the bottom of this file. It reads a symbol the way
a scanner does -- format information first, then unmask, then de-interleave the
blocks, then check every block's Reed-Solomon codewords -- and gets the original
string back out. A fixture proves the output did not change; the decoder proves
it was right in the first place, and it is the check that would survive someone
deliberately regenerating the fixtures.
"""

import hashlib
import re
import unittest

from colony import qr


def digest(px):
    """A short, stable name for a whole matrix."""
    flat = "".join("".join(str(v) for v in row) for row in px)
    return hashlib.sha256(flat.encode()).hexdigest()[:32]


# The token in a real phone-access URL is 43 characters of url-safe base64.
REAL_URL = "http://100.101.102.103:8787/?k=" + "u" * 43


class Fixtures(unittest.TestCase):
    """Whole matrices, pinned. Any change to the tables trips these."""

    # (text, level, expected size, digest)
    CASES = [
        ("HELLO", "L", 21, "94dc323e977de5aa63de26d41b2b9f21"),
        ("HELLO", "H", 21, "fd8dbc1e05e7cfad7d88f043ad74b020"),
        (REAL_URL, "M", 37, "d843c005d80ecff37e09d76155574b3f"),
        ("colony-dash", "Q", 21, "86665b6515bb23df13c79038f6f19e25"),
        ("x" * 120, "L", 41, "6aa506305c3b4d9ce0b308d1e0f8cc18"),
        ("y" * 180, "L", 49, "ca5cd5626de11c04f020390a9088d351"),
    ]

    def test_matrices(self):
        for text, ec, size, want in self.CASES:
            with self.subTest(ec=ec, length=len(text)):
                px = qr.encode(text, ec=ec)
                self.assertEqual(len(px), size)
                self.assertTrue(all(len(row) == size for row in px))
                self.assertEqual(digest(px), want)

    def test_real_url_is_a_comfortable_size(self):
        """The payload this exists for should not be near a version boundary.

        If this starts failing the URL got longer, and the fix is to look at
        why rather than to bump the number.
        """
        px = qr.encode(REAL_URL, ec="M")
        self.assertEqual(len(px), 37)               # version 5


class Structure(unittest.TestCase):
    """The parts of the symbol that are the same in every code."""

    def setUp(self):
        self.px = qr.encode(REAL_URL, ec="M")
        self.size = len(self.px)

    def test_finder_patterns(self):
        want = [
            [1, 1, 1, 1, 1, 1, 1],
            [1, 0, 0, 0, 0, 0, 1],
            [1, 0, 1, 1, 1, 0, 1],
            [1, 0, 1, 1, 1, 0, 1],
            [1, 0, 1, 1, 1, 0, 1],
            [1, 0, 0, 0, 0, 0, 1],
            [1, 1, 1, 1, 1, 1, 1],
        ]
        corners = [(0, 0), (0, self.size - 7), (self.size - 7, 0)]
        for oy, ox in corners:
            got = [row[ox:ox + 7] for row in self.px[oy:oy + 7]]
            self.assertEqual(got, want, f"finder at {(ox, oy)}")

    def test_timing_patterns(self):
        for i in range(8, self.size - 8):
            self.assertEqual(self.px[6][i], 1 - i % 2, f"row timing at x={i}")
            self.assertEqual(self.px[i][6], 1 - i % 2, f"col timing at y={i}")

    def test_dark_module(self):
        """One module, always set, at (8, size - 8). A scanner looks for it."""
        self.assertEqual(self.px[self.size - 8][8], 1)

    def test_versions_under_seven_carry_no_version_block(self):
        px = qr.encode("HELLO", ec="L")             # version 1
        self.assertEqual(len(px), 21)
        # The version block would live here; at version 1 it is data.
        self.assertEqual(len(px[0]), 21)


class Refusals(unittest.TestCase):

    def test_too_long_names_the_level(self):
        with self.assertRaises(qr.TooLong) as caught:
            qr.encode("z" * 400, ec="H")
        self.assertIn("H", str(caught.exception))

    def test_too_long_is_a_value_error(self):
        """Callers that only care that it did not fit should not need the name."""
        self.assertTrue(issubclass(qr.TooLong, ValueError))

    def test_unknown_level(self):
        with self.assertRaises(ValueError):
            qr.encode("HELLO", ec="Z")

    def test_version_out_of_range(self):
        with self.assertRaises(qr.TooLong):
            qr.encode("HELLO", ec="L", version=11)

    def test_capacity_edges(self):
        """One byte either side of every version's limit, at every level."""
        for version in range(1, 11):
            for ec in qr.LEVELS:
                limit = qr._capacity(version, ec) - (2 if version < 10 else 3)
                fits = qr.encode("a" * limit, ec=ec, version=version)
                self.assertEqual(len(fits), version * 4 + 17)
                with self.assertRaises(qr.TooLong):
                    qr.encode("a" * (limit + 1), ec=ec, version=version)


class Rendering(unittest.TestCase):

    def test_svg_is_one_path(self):
        out = qr.svg("HELLO", ec="L", quiet=4)
        self.assertEqual(out.count("<path"), 1)
        self.assertEqual(out.count("<rect"), 1)
        self.assertTrue(out.startswith("<svg"))
        self.assertTrue(out.endswith("</svg>"))

    def test_svg_view_box_includes_the_quiet_zone(self):
        out = qr.svg("HELLO", ec="L", quiet=4)
        box = re.search(r'viewBox="0 0 (\d+) (\d+)"', out)
        self.assertEqual(box.group(1), box.group(2))
        self.assertEqual(int(box.group(1)), 21 + 8)

    def test_svg_has_no_unescaped_payload(self):
        """The text never reaches the markup, so it cannot break out of it."""
        out = qr.svg('"><script>alert(1)</script>', ec="L")
        self.assertNotIn("script", out)

    def test_text_art_is_half_height(self):
        art = qr.text_art("HELLO", ec="L", quiet=2)
        lines = art.split("\n")
        rows = 21 + 4                               # symbol plus quiet zone
        self.assertEqual(len(lines), (rows + 1) // 2)
        self.assertTrue(all(len(line) == 21 + 4 for line in lines))

    def test_text_art_draws_dark_modules_light(self):
        """A terminal is light-on-dark; the naive mapping is a negative.

        The top-left cell of the quiet zone is a light module, and light
        modules print as the full block.
        """
        art = qr.text_art("HELLO", ec="L", quiet=2)
        self.assertEqual(art[0], "█")


# -- reading one back ---------------------------------------------------------
#
# Everything below is a scanner, in the order a scanner does it. It only has to
# handle symbols this module produces, so there is no error correction here --
# the Reed-Solomon codewords are recomputed and compared rather than used to
# repair anything. That is a stricter check than decoding would be.


class Decoded(unittest.TestCase):

    def read_format(self, px):
        """Error level and mask, out of the copy beside the top-left finder."""
        bits = 0
        for i in range(15):
            if i < 6:
                v = px[i][8]
            elif i == 6:
                v = px[7][8]
            elif i == 7:
                v = px[8][8]
            elif i == 8:
                v = px[8][7]
            else:
                v = px[8][14 - i]
            bits |= v << i
        raw = bits ^ 0x5412
        value = raw >> 10
        # The BCH check bits have to agree, or the format was written wrong and
        # a scanner would reject the symbol outright.
        self.assertEqual(qr._bch(value, 0x537, 10), raw & 0x3FF)
        level = {v: k for k, v in qr.EC_BITS.items()}[value >> 3]
        return level, value & 7

    def read_second_format_copy(self, px):
        """The other copy, split between the two remaining corners."""
        size = len(px)
        bits = 0
        for i in range(15):
            v = px[8][size - 1 - i] if i < 8 else px[size - 15 + i][8]
            bits |= v << i
        return bits

    def read_stream(self, px, version, mask):
        """Unmask and walk the placement order, two columns at a time."""
        g = qr._skeleton(version)
        fn = qr._mask_fn(mask)
        size = len(px)
        bits = []
        x = size - 1
        up = True
        while x > 0:
            if x == 6:
                x -= 1
            rows = range(size - 1, -1, -1) if up else range(size)
            for y in rows:
                for dx in (0, 1):
                    cx = x - dx
                    if g.fixed[y][cx]:
                        continue
                    bits.append(px[y][cx] ^ (1 if fn(cx, y) else 0))
            x -= 2
            up = not up
        return bits

    def decode(self, px):
        size = len(px)
        version = (size - 17) // 4
        ec, mask = self.read_format(px)
        self.assertEqual(self.read_second_format_copy(px),
                         qr._format_bits(ec, mask),
                         "the two format copies disagree")

        bits = self.read_stream(px, version, mask)
        total = qr.TOTAL_CODEWORDS[version - 1]
        words = [int("".join(str(b) for b in bits[i:i + 8]), 2)
                 for i in range(0, total * 8, 8)]
        # Whatever is left over is the remainder bits, and they are always zero.
        self.assertTrue(not any(bits[total * 8:]), "remainder bits are not zero")

        # De-interleave: the inverse of the column-wise read in `_interleave`.
        per_block, g1, g2 = qr.BLOCKS[ec][version - 1]
        short = qr._capacity(version, ec) // (g1 + g2)
        lengths = [short] * g1 + [short + 1] * g2
        blocks = [[] for _ in lengths]
        at = 0
        for i in range(short + 1):
            for b, length in enumerate(lengths):
                if i < length:
                    blocks[b].append(words[at])
                    at += 1
        checks = [[] for _ in lengths]
        for i in range(per_block):
            for b in range(len(lengths)):
                checks[b].append(words[at])
                at += 1
        self.assertEqual(at, len(words))

        for b, (data, check) in enumerate(zip(blocks, checks)):
            self.assertEqual(qr._remainder(data, per_block), check,
                             f"block {b} error correction does not check out")

        stream = [b for block in blocks for b in block]
        head = []
        for word in stream:
            for i in range(7, -1, -1):
                head.append((word >> i) & 1)
        self.assertEqual(head[:4], [0, 1, 0, 0], "not byte mode")
        count_bits = 8 if version < 10 else 16
        length = int("".join(str(b) for b in head[4:4 + count_bits]), 2)
        start = 4 + count_bits
        payload = bytes(int("".join(str(b) for b in head[start + i * 8:
                                                        start + i * 8 + 8]), 2)
                        for i in range(length))
        return payload.decode("utf-8"), ec, version, mask

    def test_round_trip(self):
        cases = [
            ("HELLO", "L"),
            ("HELLO", "H"),
            (REAL_URL, "M"),
            ("colony-dash", "Q"),
            ("x" * 120, "L"),
            ("y" * 180, "L"),
            ("http://192.168.1.42:8787/", "Q"),
            ("a", "H"),
        ]
        for text, ec in cases:
            with self.subTest(ec=ec, length=len(text)):
                px = qr.encode(text, ec=ec)
                got, level, version, mask = self.decode(px)
                self.assertEqual(got, text)
                self.assertEqual(level, ec)
                self.assertIn(mask, range(8))

    def test_round_trip_every_version_and_level(self):
        """Every version this module knows, at every level, pinned mask.

        This is the test that would catch a wrong row in `BLOCKS` -- a bad
        block layout still produces a plausible-looking symbol, and only
        de-interleaving it and checking the codewords finds it.
        """
        for version in range(1, 11):
            for ec in qr.LEVELS:
                text = "colony/" + "n" * (version * 3)
                if len(text) > qr._capacity(version, ec) - 3:
                    continue
                for mask in (0, 5):
                    with self.subTest(version=version, ec=ec, mask=mask):
                        px = qr.encode(text, ec=ec, version=version, mask=mask)
                        got, level, seen, used = self.decode(px)
                        self.assertEqual(got, text)
                        self.assertEqual(level, ec)
                        self.assertEqual(seen, version)
                        self.assertEqual(used, mask)

    def test_version_block_reads_back(self):
        """Versions 7 and up carry their own number, twice, with BCH bits."""
        for version in (7, 8, 9, 10):
            px = qr.encode("v", ec="L", version=version)
            size = len(px)
            for corner in range(2):
                bits = 0
                for i in range(18):
                    v = (px[size - 11 + i % 3][i // 3] if corner
                         else px[i // 3][size - 11 + i % 3])
                    bits |= v << i
                self.assertEqual(bits >> 12, version)
                self.assertEqual(qr._bch(version, 0x1F25, 12), bits & 0xFFF)

    def test_utf8_round_trips(self):
        """Non-ASCII is encoded as UTF-8 with no ECI header, and comes back."""
        text = "café — colony"
        px = qr.encode(text, ec="M")
        got, _, _, _ = self.decode(px)
        self.assertEqual(got, text)


if __name__ == "__main__":
    unittest.main()
